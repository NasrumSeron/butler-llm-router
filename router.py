"""
router.py — decides WHICH bot handles a message. The heart of Butler.

Three paths, cheapest first:

  1. Explicit command   "/calendar lunch tomorrow"   -> free, instant, always wins
  2. Keyword fast-path  "add to my calendar ..."     -> free, instant
  3. LLM                everything else              -> costs a call

The router NEVER executes anything. It returns a Route describing what it thinks
should happen; butler.py decides whether to act on it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import config
from registry import Registry, Service

log = logging.getLogger("butler.router")


# --- what the router returns -------------------------------------------------

INVOKE = "invoke"    # confident: call this bot
CLARIFY = "clarify"  # unsure: ask the user which bot they meant
CHAT = "chat"        # no bot fits: Butler answers this itself


# --- the time guard (K3/K4) --------------------------------------------------
#
# A keyword hit alone used to be enough to act — "turn on my PC at 8am
# tomorrow" hit the "pc" keyword and fired pc_power immediately, ignoring the
# "8am tomorrow". This regex catches a time or recurrence phrase in the same
# message. When it matches and the matched bot isn't exempt (config.py:
# ROUTER_TIME_GUARD_EXEMPT, default just "calendar" — its keyword + a time is
# its normal case), the keyword path steps aside and lets the LLM decide.
_TIME_PHRASE = re.compile(
    r"\btomorrow\b|\btonight\b|\blater\b|\bevery\b|\bdaily\b|\bweekly\b"
    r"|\beach\s+(?:day|morning|evening|night|week)\b"
    r"|\bat\s+\d{1,2}(?::\d{2})?"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|\bin\s+\d+\s+(?:min|mins|minute|minutes|hour|hours)\b"
    r"|\bnext\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bon\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


@dataclass
class Route:
    kind: str
    service: str | None = None
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reasoning: str = ""
    method: str = ""                      # command | keyword | llm | followup | fallback
    candidates: list[str] = field(default_factory=list)   # for CLARIFY

    # The runner-up, and how far behind it was. A narrow margin is what
    # "ambiguous" actually looks like — see _ask_llm.
    alternative: str | None = None
    alternative_confidence: float = 0.0
    clarify_reason: str = ""              # "low confidence" | "narrow margin"

    @property
    def margin(self) -> float:
        return self.confidence - self.alternative_confidence

    def describe(self) -> str:
        alt = (f" [2nd: {self.alternative} {self.alternative_confidence:.2f}]"
               if self.alternative else "")
        if self.kind == INVOKE:
            return (f"{self.method}: -> {self.service}.{self.action} "
                    f"(confidence {self.confidence:.2f}){alt} {self.reasoning}")
        if self.kind == CLARIFY:
            return (f"{self.method}: ambiguous between {self.candidates} "
                    f"({self.clarify_reason}, top {self.confidence:.2f}{alt}) "
                    f"— {self.reasoning}")
        return f"{self.method}: butler handles it — {self.reasoning}"


# --- the LLM Butler talks to (kept behind a Protocol so tests can fake it) ----

class RouterLLM(Protocol):
    def decide(self, message: str, catalog: str) -> dict[str, Any]:
        """Return {service, action, params, confidence, reasoning}. May raise."""
        ...


ROUTING_PROMPT = """You are the routing layer of a personal assistant called Butler.
Your ONLY job is to decide which specialist bot should handle the user's message.
You never answer the user's question yourself.

Here are the bots available right now:

{catalog}

The user said:
\"\"\"{message}\"\"\"

Reply with ONLY a JSON object, no markdown fence, in exactly this shape:

{{
  "service": "<bot name from the list, or null if no bot fits>",
  "action": "<action name from that bot, or null>",
  "params": {{ "<param name>": "<value taken from the user's message>" }},
  "confidence": <number between 0 and 1>,
  "alternative": "<the SECOND most suitable bot name, or null if no other bot fits at all>",
  "alternative_confidence": <number between 0 and 1>,
  "reasoning": "<one short sentence explaining the choice>"
}}

Rules:
- If no bot is a good fit (small talk, a general question, a greeting), set service to null.
- Score each bot INDEPENDENTLY, on how well it suits the message on its own merits.
  Do NOT lower your first score just because a second bot also fits — say so by
  giving that second bot a high alternative_confidence instead. Two bots scoring
  0.8 and 0.75 is a perfectly good answer and tells us more than one score of 0.5.
- alternative must be a DIFFERENT bot from service, or null.
- Only use param names listed for that action. Copy values from the user's message; never invent them.
- Confidence is about which BOT, not about whether the task will succeed.
- If the user wants a bot to act at a later time or on a repeat, and that bot acts immediately (it can't schedule), do NOT pick that bot. Pick a bot that can schedule (e.g. calendar) if it fits, else set service to null.
"""


class Router:
    def __init__(self, registry: Registry, llm: RouterLLM | None = None) -> None:
        self.registry = registry
        self.llm = llm

    # -- public ------------------------------------------------------------

    def route(self, message: str) -> Route:
        text = (message or "").strip()
        if not text:
            return Route(kind=CHAT, method="fallback", reasoning="empty message")

        # Offline bots stay in the running on purpose — see Registry.routable_services.
        services = self.registry.routable_services()
        if not services:
            return Route(kind=CHAT, method="fallback",
                         reasoning="no bots are configured, so nothing to route to")

        route = self._try_command(text, services)
        if route:
            return route

        route = self._try_keywords(text, services)
        if route:
            return route

        return self._ask_llm(text, services)

    # -- path 1: explicit command -------------------------------------------

    def _try_command(self, text: str, services: list[Service]) -> Route | None:
        """'/calendar lunch tomorrow' — the user naming the bot. Always obeyed."""
        m = re.match(r"^/(\w+)\s*(.*)$", text, flags=re.DOTALL)
        if not m:
            return None
        name, rest = m.group(1).lower(), m.group(2).strip()

        svc = next((s for s in services if s.name.lower() == name), None)
        if svc is None or not svc.public_actions():
            return None  # not a bot name; butler.py handles /help, /status etc.

        action = svc.public_actions()[0]
        return Route(
            kind=INVOKE,
            service=svc.name,
            action=action.name,
            params=self._fill_params(action.params, rest or text),
            confidence=1.0,
            reasoning="you named the bot explicitly",
            method="command",
        )

    # -- path 2: keyword fast-path -------------------------------------------

    def _try_keywords(self, text: str, services: list[Service]) -> Route | None:
        """
        Free routing. Only fires when the evidence is unambiguous:
          - exactly ONE bot matched any keyword, AND
          - that bot has exactly ONE action (so there is nothing left to choose), AND
          - the message carries no time/recurrence phrase for a non-exempt bot
            (the time guard — see _TIME_PHRASE; a bot that can't schedule must
            not act immediately on "...at 8am tomorrow").
        Anything less certain falls through to the LLM on purpose.
        """
        lowered = text.lower()
        hits: dict[str, list[str]] = {}
        for svc in services:
            matched = [kw for kw in svc.keywords if self._contains(lowered, kw)]
            if matched:
                hits[svc.name] = matched

        if len(hits) != 1:
            if len(hits) > 1 and config.VERBOSE_ROUTING:
                log.debug("keyword path declined: %d bots matched (%s)", len(hits), list(hits))
            return None

        name, matched = next(iter(hits.items()))
        svc = self.registry.get(name)
        if svc is None or len(svc.public_actions()) != 1:
            return None

        if svc.name not in config.ROUTER_TIME_GUARD_EXEMPT:
            phrase = _TIME_PHRASE.search(lowered)
            if phrase:
                if config.VERBOSE_ROUTING:
                    log.debug("time guard: %s matched %r for bot %s — keyword path "
                              "skipped, falling through to the LLM",
                              phrase.group(0), text, svc.name)
                return None

        action = svc.public_actions()[0]
        return Route(
            kind=INVOKE,
            service=svc.name,
            action=action.name,
            params=self._fill_params(action.params, text),
            confidence=0.9,
            reasoning=f"matched keyword(s) {matched}",
            method="keyword",
        )

    @staticmethod
    def _contains(haystack: str, needle: str) -> bool:
        """Whole-word-ish match so 'cal' does not match 'physical'."""
        if " " in needle:
            return needle in haystack
        return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None

    # -- path 3: the LLM ------------------------------------------------------

    def _ask_llm(self, text: str, services: list[Service]) -> Route:
        if self.llm is None:
            return Route(kind=CHAT, method="fallback",
                         reasoning="no router LLM configured and no keyword matched")

        catalog = self.build_catalog(services)
        try:
            raw = self.llm.decide(message=text, catalog=catalog)
        except Exception as exc:  # noqa: BLE001
            log.error("router LLM failed: %s", exc)
            return Route(kind=CHAT, method="llm",
                         reasoning=f"router LLM unavailable ({type(exc).__name__})")

        service = raw.get("service")
        if service in (None, "", "null"):
            return Route(kind=CHAT, method="llm",
                         confidence=float(raw.get("confidence") or 0.0),
                         reasoning=str(raw.get("reasoning", "no bot fits")))

        svc = self.registry.get(str(service))
        if svc is None or not svc.actions:
            return Route(kind=CHAT, method="llm",
                         reasoning=f"router named unknown bot '{service}'")

        public = svc.public_actions()
        action_name = str(raw.get("action") or (public[0].name if public else ""))
        action = svc.action(action_name)
        # An internal action is not something a person asks for out of the blue.
        # If the model picked one, fall back to the bot's first public action.
        if action is None or action.internal:
            action = public[0]
            action_name = action.name

        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        params = self._clean_params(action.params, raw.get("params") or {}, text)
        reasoning = str(raw.get("reasoning", "")).strip()

        # --- the runner-up ---------------------------------------------------
        alt_name = raw.get("alternative")
        alt_name = str(alt_name) if alt_name not in (None, "", "null") else None
        if alt_name == svc.name:
            alt_name = None                      # a bot can't be its own alternative
        if alt_name is not None and self.registry.get(alt_name) is None:
            alt_name = None                      # invented bot name; ignore it
        try:
            alt_confidence = float(raw.get("alternative_confidence", 0.0))
        except (TypeError, ValueError):
            alt_confidence = 0.0
        if alt_name is None:
            alt_confidence = 0.0

        base = dict(service=svc.name, action=action_name, params=params,
                    confidence=confidence, reasoning=reasoning, method="llm",
                    alternative=alt_name, alternative_confidence=alt_confidence)

        # --- when to ask rather than act --------------------------------------
        #
        # Two independent reasons, and the second is the one that matters.
        #
        # Absolute confidence catches "I don't really know" (0.4 for everything).
        # It does NOT catch the case I actually hit: the model was 0.80 sure of
        # calendar for "remind me about the CPF contribution deadline" — but it
        # was probably nearly as sure of finance. One number can't tell those
        # apart, which is why raising the threshold couldn't fix it without
        # making Butler ask about everything.
        #
        # The margin can. calendar 0.80 / finance 0.75 is a coin flip;
        # calendar 0.80 / finance 0.05 is not. Same top score, opposite meaning.
        reason = ""
        if confidence < config.ROUTER_CONFIDENCE_THRESHOLD:
            reason = "low confidence"
        elif alt_name and (confidence - alt_confidence) < config.ROUTER_MARGIN_THRESHOLD:
            reason = "narrow margin"

        if reason:
            others = [s.name for s in services if s.name not in (svc.name, alt_name)]
            candidates = [svc.name] + ([alt_name] if alt_name else []) + others
            return Route(
                kind=CLARIFY,
                candidates=candidates[:3],
                clarify_reason=reason,
                **{**base, "reasoning": reasoning or "not sure enough to act"},
            )

        return Route(kind=INVOKE, **base)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def build_catalog(services: list[Service]) -> str:
        """The bot menu, as plain text for the LLM. Built from live /capabilities."""
        blocks = []
        for svc in services:
            state = "" if svc.online else "  (currently offline — still name it if it is the right bot)"
            lines = [f"BOT: {svc.name}{state}", f"  purpose: {svc.description}"]
            if svc.keywords:
                lines.append(f"  typical words: {', '.join(svc.keywords)}")
            # Internal actions are deliberately hidden from the routing model.
            for a in svc.public_actions():
                lines.append(f"  action: {a.name} — {a.description}")
                for pname, pdesc in a.params.items():
                    lines.append(f"      param {pname}: {pdesc}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @staticmethod
    def _fill_params(spec: dict[str, str], text: str) -> dict[str, Any]:
        """
        Keyword and command paths do no extraction — they hand the bot the raw
        message and let the bot parse it. That is deliberate: parsing is domain
        knowledge and belongs to the bot, not to Butler.
        """
        if not spec:
            return {}
        return {name: text for name in spec}

    @staticmethod
    def _clean_params(spec: dict[str, str], got: dict[str, Any], text: str) -> dict[str, Any]:
        """Drop params the action never declared; backfill any it declared but the LLM omitted."""
        cleaned = {k: v for k, v in got.items() if k in spec}
        for name in spec:
            if name not in cleaned or cleaned[name] in (None, ""):
                cleaned[name] = text
        return cleaned
