"""
test_calendar_live.py — Butler <-> the REAL calendar adapter, over real HTTP.

This is the calendar adapter's acceptance test. It runs your actual service.py in a separate
process, with only Gemini and iCloud faked out, and drives the whole thing
through Butler exactly as Telegram would.

Nothing here spends an API call or writes to your calendar.

Setup — clone the calendar bot repo next to this one, or point at it:

    CALENDAR_BOT_DIR=../telegram-icloud-calendar-bot python tests/test_calendar_integration.py

If the folder isn't found the test says so and exits, rather than pretending
to pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from butler import Butler                      # noqa: E402
from registry import Registry, Service         # noqa: E402
from router import Router                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL_DIR = os.path.abspath(os.environ.get(
    "CALENDAR_BOT_DIR", os.path.join(ROOT, "..", "telegram-icloud-calendar-bot")))
PORT = 9111

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail and not condition else ""))


class StubLLM:
    def decide(self, message, catalog):
        return {"service": None, "action": None, "params": {},
                "confidence": 0.0, "reasoning": "no bot fits"}

    def chat(self, message, history=None, bots=None):
        return "Butler here."


# A launcher that imports the real service.py, then swaps out only the two
# things that touch the outside world.
LAUNCHER = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, {cal_dir!r})
    os.environ.setdefault("GEMINI_API_KEY", "test")
    os.environ.setdefault("ICLOUD_APPLE_ID", "test@example.com")
    os.environ.setdefault("ICLOUD_APP_SPECIFIC_PASSWORD", "test")
    os.environ.setdefault("DEFAULT_CALENDAR_MAP", "111:Bot Events")

    import service
    from gemini_parse import ParsedEvent

    WRITES = []

    def fake_calendars():
        return ["Bot Events", "Family", "Work"]

    def fake_parse(text, reference_dt=None, pending=None):
        low = text.lower()
        if pending is not None:
            updated = pending.model_copy()
            if "2pm" in low:
                updated.start_datetime = "2026-09-10T14:00:00+08:00"
            return updated
        return ParsedEvent(
            title="Lunch with Sarah",
            start_datetime="2026-09-10T13:00:00+08:00",
            end_datetime=None, location=None, all_day=False, notes=None)

    def fake_create(event, calendar_name=None, *, alarm_offset=None):
        WRITES.append(calendar_name)
        with open({marker!r}, "a") as fh:
            fh.write(event.title + "|" + str(calendar_name) + "\\n")
        return "https://caldav.icloud.com/fake/event.ics"

    service.calendars = fake_calendars
    service.list_calendar_names = fake_calendars
    service.parse_event_text = fake_parse
    service.create_calendar_event = fake_create

    import logging
    logging.disable(logging.CRITICAL)
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer(("127.0.0.1", {port}), service.Handler).serve_forever()
''')


def run() -> int:
    print("=" * 70)
    print("Butler <-> the real calendar adapter")
    print("=" * 70)

    if not os.path.isdir(CAL_DIR) or not os.path.exists(os.path.join(CAL_DIR, "service.py")):
        print(f"\nCannot find service.py in: {CAL_DIR}")
        print("Set CALENDAR_BOT_DIR to your calendar bot folder and rerun.")
        return 1

    marker = os.path.join(tempfile.gettempdir(), f"butler_writes_{os.getpid()}.txt")
    if os.path.exists(marker):
        os.remove(marker)

    launcher = LAUNCHER.format(cal_dir=CAL_DIR, port=PORT, marker=marker)
    proc = subprocess.Popen([sys.executable, "-c", launcher],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(1.5)
    if proc.poll() is not None:
        print("\nThe adapter failed to start:")
        print(proc.stderr.read().decode()[-1500:])
        return 1

    try:
        print("\n1. Butler discovers the calendar bot's real capabilities")
        reg = Registry()
        reg.services = {"calendar": Service(name="calendar",
                                            url=f"http://127.0.0.1:{PORT}")}
        reg.discover()
        cal = reg.get("calendar")
        check("online", bool(cal and cal.online), cal.last_error if cal else "")
        check("version says butler", "butler" in (cal.version or ""), f"{cal.version}")
        check("exactly one public action", len(cal.public_actions()) == 1,
              f"{[a.name for a in cal.public_actions()]}")
        check("every other action is internal",
              len(cal.actions) - len(cal.public_actions()) == len(cal.actions) - 1,
              f"{[a.name for a in cal.actions]}")
        check("alert and calendar menus exist",
              {"set_alert", "set_calendar", "open_menu"} <= {a.name for a in cal.actions},
              f"{[a.name for a in cal.actions]}")
        check("description warns what it does not do", "NOT" in cal.description)

        catalog = Router.build_catalog(reg.routable_services())
        check("routing menu shows only draft_event",
              "draft_event" in catalog and "confirm_event" not in catalog, catalog)

        print("\n2. A calendar message routes for free and returns a review card")
        b = Butler(reg, Router(reg, StubLLM()), chat_llm=StubLLM())
        reply = b.handle_message(111, "lunch with Sarah on Thursday, add to calendar")
        check("keyword path, no LLM", reply.route is not None
              and reply.route.method == "keyword",
              reply.route.describe() if reply.route else reply.text)
        check("the card came back", "Lunch with Sarah" in reply.text, reply.text)
        check("pre-filled the usual calendar", "Bot Events" in reply.text, reply.text)
        check("card flags the missing location", "needed" in reply.text, reply.text)
        check("Butler is holding the conversation", 111 in b.awaiting)
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("offers No location + Cancel", "No location" in labels and "Cancel" in labels,
              f"{labels}")

        print("\n3. A typed correction goes straight to the bot, no routing")
        reply = b.handle_message(111, "make it 2pm")
        check("followup path used", reply.route is not None
              and reply.route.method == "followup",
              reply.route.describe() if reply.route else "")
        check("time actually changed", "02:00 PM" in reply.text, reply.text)
        check("title carried over", "Lunch with Sarah" in reply.text, reply.text)

        print("\n4. Nothing is written until you tap Add")
        check("no write yet", not os.path.exists(marker), "a write happened too early!")

        labels = [lbl for row in reply.buttons for lbl, _ in row]
        idx = labels.index("No location")
        reply = b.handle_callback(111, f"fu:{idx}")
        check("location skipped", "(none)" in reply.text, reply.text)

        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("now offers Add to calendar", "Add to calendar" in labels, f"{labels}")

        print("\n5. Tapping Add writes exactly one event and ends the conversation")
        idx = labels.index("Add to calendar")
        reply = b.handle_callback(111, f"fu:{idx}")
        check("confirmation message", "Added" in reply.text, reply.text)
        check("written once", os.path.exists(marker)
              and len(open(marker).read().strip().splitlines()) == 1,
              open(marker).read() if os.path.exists(marker) else "no file")
        check("written to the right calendar",
              "Bot Events" in open(marker).read(), open(marker).read())
        check("conversation lock released", 111 not in b.awaiting, f"{b.awaiting}")
        check("no buttons left", reply.buttons == [], f"{reply.buttons}")

        print("\n6. After confirming, a new message routes normally again")
        reply = b.handle_message(111, "hello there")
        check("back to chat", "Butler here" in reply.text, reply.text)

        print("\n7. Cancel writes nothing")
        before = len(open(marker).read().strip().splitlines())
        reply = b.handle_message(111, "meeting with the dean Friday 10am")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        reply = b.handle_callback(111, f"fu:{labels.index('Cancel')}")
        check("cancelled", "Nothing was added" in reply.text, reply.text)
        after = len(open(marker).read().strip().splitlines())
        check("still only the one earlier write", before == after, f"{before} -> {after}")
        check("lock released", 111 not in b.awaiting)

        print("\n8. The two bugs found in real use: alert and calendar are changeable")
        reply = b.handle_message(111, "team meeting Friday 10am")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("card offers an Alert button",
              any(l.startswith("Alert:") for l in labels), f"{labels}")
        check("card offers a Calendar button even when pre-filled",
              "Calendar: Bot Events" in labels, f"{labels}")

        # --- alert
        reply = b.handle_callback(111, f"fu:{labels.index('Alert: 30 min before')}")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("alert menu opened", "1 hour before" in labels, f"{labels}")
        reply = b.handle_callback(111, f"fu:{labels.index('1 hour before')}")
        check("alert changed on the card", "1 hour before" in reply.text, reply.text)

        # --- calendar
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        reply = b.handle_callback(111, f"fu:{labels.index('Calendar: Bot Events')}")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("calendar menu opened", "Family" in labels and "Work" in labels, f"{labels}")
        reply = b.handle_callback(111, f"fu:{labels.index('Family')}")
        check("calendar changed on the card", "Family" in reply.text, reply.text)

        # --- typed attempt now opens the menu instead of doing nothing
        reply = b.handle_message(111, "change the alert to 2 hours")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        check("typing about the alert opens its menu",
              "2 hours before" in labels, f"{labels}")
        check("and says why typing alone won't do it",
              "can't change that one" in reply.text, reply.text)
        reply = b.handle_callback(111, f"fu:{labels.index('2 hours before')}")
        check("typed route reached the same result",
              "2 hours before" in reply.text, reply.text)

        # --- and it all survives to the write
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        reply = b.handle_callback(111, f"fu:{labels.index('No location')}")
        labels = [lbl for row in reply.buttons for lbl, _ in row]
        before = len(open(marker).read().strip().splitlines())
        reply = b.handle_callback(111, f"fu:{labels.index('Add to calendar')}")
        written = open(marker).read().strip().splitlines()
        check("one more event written", len(written) == before + 1, str(written))
        check("written to the calendar picked by button",
              written[-1].endswith("|Family"), written[-1])
        check("confirmation names the new alert",
              "2 hours before" in reply.text, reply.text)

    finally:
        proc.terminate()
        if os.path.exists(marker):
            os.remove(marker)

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
