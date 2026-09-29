"""
run_goldset_live.py — measures ROUTING ACCURACY against the real model.

The offline suite proves Butler's plumbing is safe. This proves the model is
actually good at picking bots. They are different questions and you need both.

You need GEMINI_API_KEY set. The stub bots must be running:

    python stubs/stub_service.py calendar 9101 &
    python stubs/stub_service.py finance  9102 &
    GEMINI_API_KEY=xxx python tests/live/run_goldset_live.py

It sends one message per gold-standard line, so a 15-line goldset costs 15
routing calls. On the Gemini free tier that is free.

READ THE OUTPUT, DON'T JUST CHECK THE SCORE. The row that matters most is any
AMBIGUOUS line the model answered confidently — that means it is guessing on
cases where it should be asking you.
"""

from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))   # repo root: config, router, ...
sys.path.insert(0, os.path.join(_HERE, ".."))         # tests/: goldset

import config                                   # noqa: E402
from llm import GeminiLLM                       # noqa: E402
from registry import Registry                   # noqa: E402
from router import CHAT, CLARIFY, INVOKE, Router  # noqa: E402
from goldset import AMBIGUOUS, EITHER, GOLD     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(_HERE))

# Gemini free tier is 15 requests/min. 22+ goldset rows can exceed that in one
# run; a 429 makes router.py fall back to CHAT, which used to make a CHAT row
# pass falsely (the LLM never actually ran). ROW_DELAY paces us under the
# limit; verdict() below scores that failure as ERR instead of hiding it.
ROW_DELAY = float(os.getenv("GOLDSET_ROW_DELAY", "4.5"))  # 60/15 = 4s + margin


def verdict(expected, route) -> tuple[str, str]:
    """Return (symbol, explanation)."""
    if route.method == "llm" and route.reasoning.startswith("router LLM unavailable"):
        return "ERR", f"LLM error: {route.reasoning}"

    if expected == EITHER:
        if route.kind == CHAT:
            return "BAD", "answered it itself instead of using a bot"
        return "OK ", f"{route.service or 'asked'} — either was acceptable"

    if expected == AMBIGUOUS:
        if route.kind == CLARIFY:
            return "OK ", f"asked ({route.clarify_reason}), as it should"
        gap = (f", runner-up {route.alternative} {route.alternative_confidence:.0%}"
               if route.alternative else ", no runner-up offered")
        return "BAD", (f"acted on {route.service} at {route.confidence:.0%}"
                       f"{gap} — margin {route.margin:.0%}, needs to be under "
                       f"{config.ROUTER_MARGIN_THRESHOLD:.0%} to trigger a prompt")

    if isinstance(expected, tuple):
        # A tuple names a specific SET of acceptable outcomes (unlike EITHER,
        # which means "any bot is fine"). None in the tuple means chat counts.
        if route.kind == CHAT:
            if None in expected:
                return "OK ", "handled by Butler — one of the acceptable outcomes"
            return "BAD", f"answered it itself, expected one of {expected}"
        if route.kind == INVOKE and route.service in expected:
            return "OK ", f"{route.method}, {route.confidence:.0%}"
        if route.kind == CLARIFY:
            return "MEH", f"asked instead of picking one of {expected}"
        return "BAD", f"got {route.describe()}, expected one of {expected}"

    if expected is None:
        if route.kind == CHAT:
            return "OK ", "handled by Butler"
        return "BAD", f"sent to {route.service} instead of answering directly"

    if route.kind == INVOKE and route.service == expected:
        return "OK ", f"{route.method}, {route.confidence:.0%}"
    if route.kind == CLARIFY and route.service == expected:
        return "MEH", f"right bot but only {route.confidence:.0%} sure, so it asked"
    if route.kind == CHAT:
        return "BAD", f"answered it itself instead of using {expected}"
    return "BAD", f"went to {route.service}, expected {expected}"


def main() -> int:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        print("GEMINI_API_KEY is not set.")
        return 1

    # config.SERVICES_FILE honours BUTLER_SERVICES_FILE, so this works both on
    # the laptop (services.yaml, 127.0.0.1) and inside the container
    # (services.docker.yaml, Docker names). Hardcoding "services.yaml" here meant
    # a run inside Docker found no bots at all.
    reg = Registry(services_file=config.SERVICES_FILE)
    reg.load()
    reg.discover()
    if not reg.online_services():
        print("No bots answered. Start the stubs first:")
        print("  python stubs/stub_service.py calendar 9101 &")
        print("  python stubs/stub_service.py finance  9102 &")
        return 1

    router = Router(reg, GeminiLLM(api_key=key))

    tally = {"OK ": 0, "MEH": 0, "BAD": 0, "ERR": 0}
    bad_rows = []
    err_rows = []

    print(f"\nModel: {config.ROUTER_MODEL}")
    print(f"confidence threshold: {config.ROUTER_CONFIDENCE_THRESHOLD}   "
          f"margin threshold: {config.ROUTER_MARGIN_THRESHOLD}   "
          f"row delay: {ROW_DELAY}s")
    print("=" * 92)
    print(f"{'':4} {'message':<52} {'expected':<10} explanation")
    print("-" * 92)

    for message, expected in GOLD:
        route = router.route(message)
        symbol, why = verdict(expected, route)
        tally[symbol] += 1
        if expected == AMBIGUOUS:
            label = "ask me"
        elif expected == EITHER:
            label = "either"
        elif isinstance(expected, tuple):
            label = "|".join((e[:3] if e else "chat") for e in expected)
        else:
            label = expected if expected else "chat"
        print(f"{symbol}  {message[:52]:<52} {label:<10} {why}")
        if symbol == "BAD":
            bad_rows.append((message, expected, route))
        elif symbol == "ERR":
            err_rows.append((message, expected, route))
        time.sleep(ROW_DELAY)

    print("=" * 92)
    total = len(GOLD)
    print(f"OK {tally['OK ']}/{total}   borderline {tally['MEH']}   "
          f"wrong {tally['BAD']}   errors {tally['ERR']}")

    if err_rows:
        print("\nERR rows — the LLM call itself failed (likely a 429; rerun or raise "
              "GOLDSET_ROW_DELAY, not a routing bug):")
        for message, expected, route in err_rows:
            print(f"\n  {message!r}")
            print(f"    expected: {expected}")
            print(f"    got:      {route.describe()}")

    if bad_rows:
        print("\nWrong routes, in detail — these are what to fix:")
        for message, expected, route in bad_rows:
            print(f"\n  {message!r}")
            print(f"    expected: {expected}")
            print(f"    got:      {route.describe()}")
        asked_when_it_should_not = [r for _, e, r in bad_rows if e == AMBIGUOUS]
        if asked_when_it_should_not:
            margins = [r.margin for r in asked_when_it_should_not if r.alternative]
            if margins:
                print(f"\n  Narrowest margin among those: {min(margins):.0%}. "
                      f"Setting ROUTER_MARGIN_THRESHOLD just above it in config.py "
                      f"would turn them into prompts.")
            else:
                print("\n  None of those offered a runner-up at all — the model sees "
                      "no second candidate, so the margin rule cannot fire. Fix the "
                      "bot descriptions instead (rule 1 below).")

        print("\nUsual fixes, in order of how often they work:")
        print("  1. Sharpen the bot's `description` in its /capabilities — say what it does NOT do.")
        print("  2. For messages that SHOULD ask: raise ROUTER_MARGIN_THRESHOLD.")
        print("  3. Add or remove `keywords` on the bot.")
        print("  4. Raise ROUTER_CONFIDENCE_THRESHOLD so it asks more across the board.")
        print("  5. Only if all of those fail: change the model.")

    return 1 if (tally["BAD"] or tally["ERR"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
