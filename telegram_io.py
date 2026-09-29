"""
telegram_io.py — every HTTP call to Telegram, hand-rolled with `requests`.

No framework on purpose: you can read the whole thing. Every line here maps to one
documented Telegram Bot API method, so you can read this file next to
https://core.telegram.org/bots/api and follow it exactly.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

import config

log = logging.getLogger("butler.telegram")

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session = requests.Session()

    def _call(self, method: str, payload: dict[str, Any] | None = None,
              timeout: int = 30) -> dict[str, Any]:
        url = API.format(token=self.token, method=method)
        resp = self.session.post(url, json=payload or {}, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body}")
        return body.get("result")

    # -- reading -------------------------------------------------------------

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        """
        Long polling. Telegram holds the connection open until something happens
        or POLL_TIMEOUT_SECONDS passes. This is why Butler needs no open port:
        the server dials out, nothing dials in.
        """
        payload: dict[str, Any] = {
            "timeout": config.POLL_TIMEOUT_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload,
                          timeout=config.POLL_TIMEOUT_SECONDS + 10) or []

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", timeout=10)

    # -- writing -------------------------------------------------------------

    def send(self, chat_id: int, text: str,
             buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """
        buttons is a list of rows; each row is a list of (label, callback_data).
        callback_data is capped by Telegram at 64 bytes — keep it short.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        try:
            self._call("sendMessage", payload)
        except RuntimeError as exc:
            # Markdown in a bot's reply can be malformed (a stray '*' or '_').
            # Never lose the message over formatting — resend as plain text.
            log.warning("sendMessage failed (%s); retrying without Markdown", exc)
            payload.pop("parse_mode", None)
            self._call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Stops the little spinner on the tapped button."""
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text}, timeout=10)
        except Exception as exc:  # noqa: BLE001 — cosmetic only, never fail a turn for it
            log.debug("answerCallbackQuery failed: %s", exc)
