"""
llm.py — the only file that talks to Gemini.

Two jobs:
  decide()  routing decisions  (JSON out)
  chat()    Butler's own small-talk replies (text out)

Kept in one small file so the whole LLM surface is auditable in one place, and so
swapping provider later means editing this file only.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import config

log = logging.getLogger("butler.llm")

# Running totals, printed by /status. This is evaluation criterion #4 (cost),
# made real rather than estimated.
usage = {"decide_calls": 0, "chat_calls": 0, "keyword_saves": 0}


class GeminiLLM:
    def __init__(self, api_key: str, model: str = config.ROUTER_MODEL) -> None:
        # Imported here, not at module top, so the offline tests never need the SDK.
        from google import genai
        from google.genai import types

        self._types = types
        # google-genai does NOT retry by default — verified the hard way on the
        # calendar bot when a live 503 killed a request. Configure it explicitly.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=3, initial_delay=1.0)
            ),
        )
        self.model = model

    # -- routing -------------------------------------------------------------

    def decide(self, message: str, catalog: str) -> dict[str, Any]:
        from router import ROUTING_PROMPT

        prompt = ROUTING_PROMPT.format(catalog=catalog, message=message)
        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=self._types.GenerateContentConfig(
                temperature=0.0,               # routing is a lookup, not a creative act
                response_mime_type="application/json",
                max_output_tokens=400,
            ),
        )
        usage["decide_calls"] += 1
        return _parse_json(resp.text)

    # -- small talk ----------------------------------------------------------

    def chat(self, message: str, history: list[tuple[str, str]] | None = None,
             bots: str | None = None) -> str:
        parts = [config.BUTLER_PERSONA, ""]

        # Without this, the chat path had NO idea any bot existed — the registry
        # was only ever read by the /bots command. So "how many bots do you
        # have?" was answered from the persona alone, and the persona's own
        # "never invent information" rule made it say "none". Honest, but wrong.
        if bots:
            parts.append("The bots you currently manage:")
            parts.append(bots)
            parts.append(
                "When asked what you can do or which bots you have, answer from "
                "that list and nothing else. Never invent a bot. If the list is "
                "empty, say you have no bots connected right now."
            )
            parts.append("")

        for role, content in (history or [])[-6:]:
            parts.append(f"{role}: {content}")
        parts.append(f"User: {message}")
        parts.append("Butler:")

        resp = self.client.models.generate_content(
            model=self.model,
            contents="\n".join(parts),
            config=self._types.GenerateContentConfig(
                temperature=0.4, max_output_tokens=500
            ),
        )
        usage["chat_calls"] += 1
        return (resp.text or "").strip() or "I don't have an answer for that."


def _parse_json(text: str | None) -> dict[str, Any]:
    """
    Models sometimes wrap JSON in a ```json fence even when asked not to.
    Strip it rather than fail the whole turn.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("router LLM returned an empty response")

    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"router LLM returned non-JSON: {raw[:200]!r}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"router LLM returned {type(parsed).__name__}, expected an object")
    return parsed
