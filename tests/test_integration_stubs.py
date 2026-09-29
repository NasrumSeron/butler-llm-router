"""
test_integration_stubs.py — real HTTP, real discovery, real invoke.

Starts the two stub bots as actual subprocesses on localhost, then drives the
whole Butler stack against them. No Telegram, no Gemini, no server.

This is the test that proves CONTRACT.md is implementable and that Butler's
discovery genuinely learns capabilities from a running bot rather than from
anything hard-coded.

Run:  python tests/test_integration_stubs.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                    # noqa: E402
from butler import Butler                        # noqa: E402
from registry import Registry                    # noqa: E402
from router import Router                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STUB = os.path.join(ROOT, "stubs", "stub_service.py")

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail and not condition else ""))


class StubLLM:
    """Stands in for Gemini. Deterministic, so the test measures plumbing not model."""

    def decide(self, message: str, catalog: str) -> dict:
        low = message.lower()
        if "balance" in low or "worth" in low:
            return {"service": "finance", "action": "query_balance",
                    "params": {"question": message}, "confidence": 0.92,
                    "reasoning": "asking about a holding"}
        if any(w in low for w in ("cpf", "premium", "loan", "invest")):
            return {"service": "finance", "action": "log_transaction",
                    "params": {"text": message}, "confidence": 0.88,
                    "reasoning": "recording money movement"}
        # A real model must be able to say "none of these" — so the double must too,
        # otherwise the chat path is never exercised.
        return {"service": None, "action": None, "params": {},
                "confidence": 0.0, "reasoning": "no bot fits"}

    def chat(self, message: str, history=None, bots=None) -> str:
        return "Butler here. No bot handles that."


def start_stubs() -> list[subprocess.Popen]:
    procs = [
        subprocess.Popen([sys.executable, STUB, "calendar", "9101"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, STUB, "finance", "9102"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    time.sleep(1.2)   # let the sockets bind
    return procs


def run() -> int:
    print("=" * 68)
    print("Butler — integration against stub bots")
    print("=" * 68)

    procs = start_stubs()
    try:
        print("\n1. Discovery learns capabilities over real HTTP")
        reg = Registry(services_file=os.path.join(ROOT, "services.yaml"))
        reg.load()
        reg.discover()

        cal = reg.get("calendar")
        fin = reg.get("finance")
        check("calendar came online", bool(cal and cal.online),
              cal.last_error if cal else "missing")
        check("finance came online", bool(fin and fin.online),
              fin.last_error if fin else "missing")
        check("learned calendar's description from the bot itself",
              bool(cal and "iCloud" in cal.description), cal.description if cal else "")
        check("learned finance has 2 actions", bool(fin and len(fin.actions) == 2),
              f"{[a.name for a in fin.actions] if fin else []}")
        check("learned keywords", bool(cal and "meeting" in cal.keywords),
              f"{cal.keywords if cal else []}")

        print("\n2. The catalog handed to the LLM is built from live discovery")
        catalog = Router.build_catalog(reg.online_services())
        check("catalog names both bots",
              "BOT: calendar" in catalog and "BOT: finance" in catalog)
        check("catalog carries params",
              "param text:" in catalog and "param question:" in catalog)

        print("\n3. Keyword route -> real HTTP invoke -> reply")
        b = Butler(reg, Router(reg, StubLLM()), chat_llm=StubLLM())
        reply = b.handle_message(1, "add a meeting with the project team Thursday 3pm")
        check("stub actually ran the action",
              "calendar stub" in reply.text and "create_event" in reply.text, reply.text)
        check("route was free", reply.route is not None and reply.route.method == "keyword",
              reply.route.describe() if reply.route else "")

        print("\n4. Explicit command -> real HTTP invoke")
        reply = b.handle_message(1, "/finance log my CPF top up of 500")
        check("reached the finance stub", "finance stub" in reply.text, reply.text)

        print("\n5. LLM route picks the right action on a multi-action bot")
        reply = b.handle_message(1, "what is my portfolio worth right now")
        check("chose query_balance", "query_balance" in reply.text, reply.text)

        print("\n6. A bot returning ok:false is reported honestly")
        reply = b.handle_message(1, "/calendar please explode now")
        check("failure surfaced", "on purpose" in reply.text, reply.text)
        check("blamed the right bot", "calendar" in reply.text, reply.text)

        print("\n7. A dead bot is detected, not guessed at")
        procs[0].terminate()
        procs[0].wait(timeout=5)
        reg._last_health_check = 0        # force a refresh
        reg.discover()
        cal = reg.get("calendar")
        check("calendar now offline", bool(cal and not cal.online))
        reply = b.handle_message(1, "/bots")
        check("/bots shows it as down", "🔴" in reply.text, reply.text)
        reply = b.handle_message(1, "book a meeting Friday 10am")
        check("still routed to calendar, not to whoever is alive",
              reply.route is not None and reply.route.service == "calendar",
              reply.route.describe() if reply.route else reply.text)
        check("told the user the calendar bot is down",
              "isn't responding" in reply.text, reply.text)
        check("did not silently hand it to finance",
              "finance" not in reply.text.lower(), reply.text)

        print("\n8. Butler answers when no bot fits")
        reply = b.handle_message(1, "what's the difference between an API and an SDK")
        check("fell through to chat", "Butler here" in reply.text, reply.text)

    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()

    print("\n" + "=" * 68)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
