"""
butler.py — orchestration. Ties registry + router + telegram together.

The whole of Butler's behaviour is handle_message(). Read that one function and
you understand the bot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import config
import llm as llm_module
from registry import Registry
from router import CHAT, CLARIFY, INVOKE, Route, Router

log = logging.getLogger("butler.core")

# How long a bot's hold on the conversation lasts if the bot doesn't say.
DEFAULT_FOLLOWUP_SECONDS = 600


@dataclass
class Reply:
    """What Butler wants to say back. Kept separate from Telegram so tests need no network."""

    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    route: Route | None = None


class Butler:
    def __init__(self, registry: Registry, router: Router, chat_llm: Any | None = None) -> None:
        self.registry = registry
        self.router = router
        self.chat_llm = chat_llm
        # Short per-user conversation memory. In-memory only (lost on restart).
        self.history: dict[int, list[tuple[str, str]]] = {}
        # Pending clarifications: user_id -> the route awaiting a button tap.
        self.pending: dict[int, Route] = {}
        # Conversation locks: user_id -> {service, action, params, buttons, expires}.
        # While one is held, the user's next message goes straight to that bot
        # instead of through the router. Set by a bot returning `followup`.
        self.awaiting: dict[int, dict[str, Any]] = {}

    # -- the main entry point -------------------------------------------------

    def handle_message(self, user_id: int, text: str) -> Reply:
        text = (text or "").strip()

        if text.startswith("/") and self._is_builtin(text):
            return self._builtin(text, user_id)

        # A bot mid-conversation gets the next message, no routing, no LLM.
        # "make it 2pm" is meaningless to a router but obvious to the bot that
        # just showed you a draft. Explicit /commands still break out.
        held = self._active_followup(user_id)
        if held and not text.startswith("/"):
            return self._continue_followup(user_id, held, text)

        self.registry.refresh_if_stale()
        route = self.router.route(text)

        if config.VERBOSE_ROUTING:
            log.info("ROUTE %r -> %s", text[:60], route.describe())

        # Count the turns that cost nothing, so /status shows what the fast-path saved.
        if route.method in ("keyword", "command"):
            llm_module.usage["keyword_saves"] += 1

        if route.kind == INVOKE:
            return self._do_invoke(user_id, text, route)

        if route.kind == CLARIFY:
            self.pending[user_id] = route
            names = route.candidates or [route.service or "?"]
            buttons = [[(n, f"pick:{n}") for n in names[:3]]]
            buttons.append([("Just answer me", "pick:__chat__")])
            return Reply(
                text=(f"I'm not sure who should handle that — I'd guess "
                      f"*{route.service}* but only {route.confidence:.0%} sure.\nWho do you want?"),
                buttons=buttons,
                route=route,
            )

        return self._do_chat(user_id, text, route)

    # -- conversation locks ---------------------------------------------------

    def _active_followup(self, user_id: int) -> dict[str, Any] | None:
        held = self.awaiting.get(user_id)
        if not held:
            return None
        if time.time() > held["expires"]:
            log.info("follow-up for %s expired; releasing the lock", user_id)
            self.awaiting.pop(user_id, None)
            return None
        return held

    def _store_followup(self, user_id: int, service: str, followup: dict[str, Any]) -> None:
        """Record a bot's request to receive the user's next message."""
        if not followup or not followup.get("action"):
            self.awaiting.pop(user_id, None)
            return
        try:
            ttl = float(followup.get("expires_in") or DEFAULT_FOLLOWUP_SECONDS)
        except (TypeError, ValueError):
            ttl = DEFAULT_FOLLOWUP_SECONDS

        buttons = [b for b in (followup.get("buttons") or []) if isinstance(b, dict) and b.get("action")]
        self.awaiting[user_id] = {
            "service": service,
            "action": str(followup["action"]),
            "params": dict(followup.get("params") or {}),
            "buttons": buttons[:9],          # Telegram inline keyboards get silly beyond this
            "expires": time.time() + ttl,
        }

    def _continue_followup(self, user_id: int, held: dict[str, Any], text: str) -> Reply:
        svc = self.registry.get(held["service"])
        if svc is None:
            self.awaiting.pop(user_id, None)
            return Reply(text="That conversation's bot has gone away.")

        action = svc.action(held["action"])
        params = dict(held["params"])
        # Fill any declared param the bot didn't pre-set with what was typed.
        for name in (action.params if action else {}):
            params.setdefault(name, text)
        if not params:
            params = {"text": text}

        route = Route(kind=INVOKE, service=svc.name, action=held["action"], params=params,
                      confidence=1.0, reasoning="continuing an open conversation",
                      method="followup")
        return self._do_invoke(user_id, text, route)

    def _followup_buttons(self, user_id: int) -> list[list[tuple[str, str]]]:
        held = self.awaiting.get(user_id)
        if not held or not held["buttons"]:
            return []
        row: list[tuple[str, str]] = []
        rows: list[list[tuple[str, str]]] = []
        for index, button in enumerate(held["buttons"]):
            # callback_data is capped at 64 bytes by Telegram, so we send an
            # index and look the real action up in self.awaiting on the tap.
            row.append((str(button.get("label", f"Option {index + 1}")), f"fu:{index}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    # -- callbacks ------------------------------------------------------------

    def handle_callback(self, user_id: int, data: str) -> Reply:
        """A button tap."""
        if data.startswith("fu:"):
            return self._followup_button(user_id, data)

        if not data.startswith("pick:"):
            return Reply(text="I don't recognise that button.")

        choice = data.split(":", 1)[1]
        route = self.pending.pop(user_id, None)
        if route is None:
            return Reply(text="That question has already been answered.")

        original = str(route.params.get("text") or route.reasoning or "")

        if choice == "__chat__":
            return self._do_chat(user_id, original, route)

        svc = self.registry.get(choice)
        if svc is None or not svc.online:
            return Reply(text=f"The {choice} bot isn't available right now.")

        action = svc.action(route.action or "") or svc.public_actions()[0]
        if action.internal:
            action = svc.public_actions()[0]
        params = {name: original for name in action.params}
        corrected = Route(
            kind=INVOKE, service=svc.name, action=action.name, params=params,
            confidence=1.0, reasoning="you picked it", method="button",
        )
        return self._do_invoke(user_id, original, corrected)

    def _followup_button(self, user_id: int, data: str) -> Reply:
        held = self._active_followup(user_id)
        if held is None:
            return Reply(text="That's expired — send it again.")
        try:
            button = held["buttons"][int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            return Reply(text="I don't recognise that button.")

        route = Route(
            kind=INVOKE, service=held["service"], action=str(button["action"]),
            params=dict(button.get("params") or {}), confidence=1.0,
            reasoning="you tapped a button", method="followup",
        )
        return self._do_invoke(user_id, str(button.get("label", "")), route)

    # -- the three outcomes ---------------------------------------------------

    def _do_invoke(self, user_id: int, text: str, route: Route) -> Reply:
        result = self.registry.invoke(
            service_name=route.service or "",
            action=route.action or "",
            params=route.params,
            user_id=user_id,
            original_message=text,
        )
        self._remember(user_id, "User", text)

        if result.ok:
            self._remember(user_id, "Butler", result.reply)
            # The bot either asks to keep the conversation, or releases it.
            self._store_followup(user_id, route.service or "", result.followup)
            return Reply(text=result.reply,
                         buttons=self._followup_buttons(user_id),
                         route=route)

        # A failure always releases the lock — otherwise a broken bot could
        # swallow every message you send from then on.
        self.awaiting.pop(user_id, None)
        return Reply(
            text=f"_{route.service}_: {result.error}",
            route=route,
        )

    def _do_chat(self, user_id: int, text: str, route: Route) -> Reply:
        if self.chat_llm is None:
            return Reply(
                text="No bot handles that, and I have no language model configured "
                     "to answer it myself.",
                route=route,
            )
        try:
            answer = self.chat_llm.chat(text, self.history.get(user_id, []),
                                        bots=self.bots_summary())
        except Exception as exc:  # noqa: BLE001
            log.error("chat failed: %s", exc)
            return Reply(text=f"I couldn't answer that ({type(exc).__name__}).", route=route)

        self._remember(user_id, "User", text)
        self._remember(user_id, "Butler", answer)
        return Reply(text=answer, route=route)

    def bots_summary(self) -> str:
        """
        A compact list of the bots, for the chat prompt.

        Deliberately shorter than the routing catalog: chat only needs to know
        what exists and roughly what it's for, not every action and parameter.
        Keeping it short keeps the token cost of small talk down.
        """
        lines = []
        for svc in self.registry.services.values():
            if not svc.enabled:
                continue
            state = "" if svc.online else " [not responding right now]"
            desc = (svc.description or "").split(".")[0].strip()
            lines.append(f"- {svc.name}{state}: {desc or 'no description available'}")
        return "\n".join(lines)

    def _remember(self, user_id: int, role: str, content: str) -> None:
        turns = self.history.setdefault(user_id, [])
        turns.append((role, content))
        del turns[:-12]   # keep the last 12 turns

    # -- built-in commands ----------------------------------------------------

    BUILTINS = {"/start", "/help", "/status", "/bots", "/cancel"}

    def _is_builtin(self, text: str) -> bool:
        return text.split()[0].lower() in self.BUILTINS

    def _builtin(self, text: str, user_id: int = 0) -> Reply:
        cmd = text.split()[0].lower()

        if cmd == "/cancel":
            held = self.awaiting.pop(user_id, None)
            self.pending.pop(user_id, None)
            if held:
                return Reply(text=f"Dropped the open conversation with _{held['service']}_.")
            return Reply(text="Nothing open to cancel.")

        if cmd in ("/start", "/help"):
            lines = [
                "*Butler* — your front door to every bot on the server.",
                "",
                "Just talk to me normally and I'll pass it to the right bot.",
                "",
                "To force a specific bot, name it: `/calendar lunch with Sarah Tuesday 1pm`",
                "",
                "`/bots` — who I can reach",
                "`/status` — health and usage",
                "`/cancel` — drop an open conversation",
            ]
            return Reply(text="\n".join(lines))

        if cmd == "/bots":
            if not self.registry.services:
                return Reply(text="I have no bots configured at all.")
            lines = ["*Bots I know about*", ""]
            for svc in self.registry.services.values():
                mark = "🟢" if svc.online else ("⚪" if not svc.enabled else "🔴")
                lines.append(f"{mark} *{svc.name}* — {svc.description or svc.last_error}")
                for a in svc.public_actions():
                    lines.append(f"    · `{a.name}` — {a.description}")
                hidden = len(svc.actions) - len(svc.public_actions())
                if hidden:
                    lines.append(f"    _(+{hidden} follow-up action(s), not directly askable)_")
            return Reply(text="\n".join(lines))

        # /status
        online = self.registry.online_services()
        u = llm_module.usage
        lines = [
            "*Butler status*",
            "",
            f"Bots online: {len(online)} of {len(self.registry.services)}",
            f"Routing LLM calls: {u['decide_calls']}",
            f"Chat LLM calls: {u['chat_calls']}",
            f"Free keyword routes: {u['keyword_saves']}",
            f"Open conversations: {len(self.awaiting)}",
        ]
        offline = [s for s in self.registry.services.values() if s.enabled and not s.online]
        if offline:
            lines.append("")
            lines.append("*Not responding:*")
            for s in offline:
                lines.append(f"· {s.name} — {s.last_error}")
        return Reply(text="\n".join(lines))
