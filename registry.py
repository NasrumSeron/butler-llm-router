"""
registry.py — Butler's address book, in code.

Responsibilities, and nothing else:
  1. Read services.yaml.
  2. Ask each enabled bot for /health and /capabilities.
  3. Hold the answers in memory so router.py can read them.
  4. Call /invoke when told to.

It does NOT decide anything. Deciding is router.py's job.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml

import config

log = logging.getLogger("butler.registry")


@dataclass
class Action:
    """One thing a bot can do."""

    name: str
    description: str
    params: dict[str, str] = field(default_factory=dict)
    internal: bool = False
    # internal=True means "reachable, but keep it out of the routing menu".
    # A bot like the calendar one has one action a person actually asks for
    # (draft_event) and several that only make sense as follow-ups
    # (set_calendar, confirm_event...). Showing all of them to the routing
    # model is how you teach it to misroute. See CONTRACT.md §6.


@dataclass
class Service:
    """One bot Butler knows about."""

    name: str
    url: str
    enabled: bool = True

    # Filled in by discovery. Until then the bot is unusable.
    online: bool = False
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    version: str = ""
    last_error: str = ""

    def action(self, name: str) -> Action | None:
        for a in self.actions:
            if a.name == name:
                return a
        return None

    def public_actions(self) -> list[Action]:
        """The actions a person can ask for directly — what routing sees."""
        return [a for a in self.actions if not a.internal]


@dataclass
class InvokeResult:
    ok: bool
    reply: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    followup: dict[str, Any] = field(default_factory=dict)
    # A bot saying "I'm mid-conversation — send me the next thing they type."
    # See CONTRACT.md §5.


class Registry:
    def __init__(self, services_file: str = config.SERVICES_FILE) -> None:
        self.services_file = services_file
        self.services: dict[str, Service] = {}
        self._last_health_check = 0.0

    # -- loading ------------------------------------------------------------

    def load(self) -> None:
        """Read services.yaml. Raises if the file is malformed — better loud than silent."""
        with open(self.services_file, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        entries = raw.get("services") or []
        if not isinstance(entries, list):
            raise ValueError("services.yaml: 'services' must be a list")

        loaded: dict[str, Service] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"services.yaml: each service must be a mapping, got {entry!r}")
            for required in ("name", "url"):
                if required not in entry:
                    raise ValueError(f"services.yaml: entry {entry!r} is missing '{required}'")
            svc = Service(
                name=str(entry["name"]).strip(),
                url=str(entry["url"]).rstrip("/"),
                enabled=bool(entry.get("enabled", True)),
            )
            if svc.name in loaded:
                raise ValueError(f"services.yaml: duplicate service name '{svc.name}'")
            loaded[svc.name] = svc

        self.services = loaded
        log.info("Loaded %d service(s) from %s", len(loaded), self.services_file)

    # -- discovery ----------------------------------------------------------

    def discover(self) -> None:
        """Ask every enabled bot what it is and what it can do."""
        for svc in self.services.values():
            if not svc.enabled:
                svc.online = False
                svc.last_error = "disabled in services.yaml"
                continue
            self._discover_one(svc)
        self._last_health_check = time.time()

        online = [s.name for s in self.services.values() if s.online]
        offline = [s.name for s in self.services.values() if s.enabled and not s.online]
        log.info("Online: %s", ", ".join(online) or "(none)")
        if offline:
            log.warning("Offline: %s", ", ".join(offline))

    def _discover_one(self, svc: Service) -> None:
        try:
            health = requests.get(
                f"{svc.url}/health", timeout=config.DISCOVERY_TIMEOUT_SECONDS
            )
            health.raise_for_status()
            hbody = health.json()
            if not hbody.get("ok"):
                raise ValueError(f"/health returned ok=false: {hbody}")
            svc.version = str(hbody.get("version", ""))

            caps = requests.get(
                f"{svc.url}/capabilities", timeout=config.DISCOVERY_TIMEOUT_SECONDS
            )
            caps.raise_for_status()
            cbody = caps.json()

            svc.description = str(cbody.get("description", "")).strip()
            svc.keywords = [str(k).lower().strip() for k in cbody.get("keywords", []) if str(k).strip()]
            svc.actions = [
                Action(
                    name=str(a["name"]),
                    description=str(a.get("description", "")),
                    params={str(k): str(v) for k, v in (a.get("params") or {}).items()},
                    internal=bool(a.get("internal", False)),
                )
                for a in cbody.get("actions", [])
                if isinstance(a, dict) and a.get("name")
            ]

            if not svc.actions:
                raise ValueError("/capabilities returned no actions")
            if not svc.public_actions():
                raise ValueError("/capabilities returned only internal actions — "
                                 "nothing a person could ever ask for")
            if not svc.description:
                raise ValueError("/capabilities returned an empty description")

            svc.online = True
            svc.last_error = ""
            log.info(
                "  %-12s online  v%-6s %d action(s), %d keyword(s)",
                svc.name, svc.version or "?", len(svc.actions), len(svc.keywords),
            )

        except Exception as exc:  # noqa: BLE001 — we want every failure mode here
            svc.online = False
            svc.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("  %-12s OFFLINE (%s)", svc.name, svc.last_error)

    def refresh_if_stale(self) -> None:
        if time.time() - self._last_health_check > config.HEALTH_REFRESH_SECONDS:
            log.debug("Health cache stale, re-discovering")
            self.discover()

    # -- reading ------------------------------------------------------------

    def online_services(self) -> list[Service]:
        return [s for s in self.services.values() if s.online]

    def routable_services(self) -> list[Service]:
        """
        Every bot Butler is *supposed* to have, whether or not it answered.

        The router deliberately considers offline bots. If the calendar bot is
        down, "book a meeting Friday" must still route TO calendar so Butler can
        say "that's the calendar bot and it isn't responding" — rather than
        handing your meeting to whichever bot happens to still be alive.
        Discovered the hard way: with calendar killed, the router confidently
        sent a meeting request to the finance bot.
        """
        return [s for s in self.services.values() if s.enabled and s.actions]

    def get(self, name: str) -> Service | None:
        return self.services.get(name)

    # -- calling ------------------------------------------------------------

    def invoke(
        self,
        service_name: str,
        action: str,
        params: dict[str, Any],
        user_id: int,
        original_message: str,
    ) -> InvokeResult:
        svc = self.services.get(service_name)
        if svc is None:
            return InvokeResult(ok=False, error=f"I don't know a bot called '{service_name}'.")
        if not svc.online:
            return InvokeResult(
                ok=False,
                error=f"The {svc.name} bot isn't responding right now ({svc.last_error or 'offline'}).",
            )
        if svc.action(action) is None:
            known = ", ".join(a.name for a in svc.actions)
            return InvokeResult(
                ok=False,
                error=f"The {svc.name} bot has no action '{action}'. It can do: {known}.",
            )

        request_id = uuid.uuid4().hex[:8]
        payload = {
            "action": action,
            "params": params,
            "request_id": request_id,
            "user_id": user_id,
            "original_message": original_message,
        }

        log.info("-> %s.%s [%s] params=%s", svc.name, action, request_id, params)
        try:
            resp = requests.post(
                f"{svc.url}/invoke", json=payload, timeout=config.INVOKE_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.Timeout:
            log.error("<- %s.%s [%s] TIMEOUT", svc.name, action, request_id)
            return InvokeResult(
                ok=False,
                error=f"The {svc.name} bot took longer than "
                      f"{config.INVOKE_TIMEOUT_SECONDS}s and I gave up waiting.",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("<- %s.%s [%s] FAILED %s", svc.name, action, request_id, exc)
            return InvokeResult(ok=False, error=f"The {svc.name} bot errored: {exc}")

        if body.get("ok"):
            reply = str(body.get("reply", "")).strip() or "(the bot returned an empty reply)"
            followup = body.get("followup") or {}
            if not isinstance(followup, dict):
                followup = {}
            log.info("<- %s.%s [%s] ok%s", svc.name, action, request_id,
                     " (awaiting follow-up)" if followup else "")
            return InvokeResult(ok=True, reply=reply, data=body.get("data") or {},
                                followup=followup)

        err = str(body.get("error", "")).strip() or "the bot reported a failure with no reason"
        log.info("<- %s.%s [%s] not-ok: %s", svc.name, action, request_id, err)
        return InvokeResult(ok=False, error=err)
