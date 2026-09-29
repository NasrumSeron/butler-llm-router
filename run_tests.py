"""Run Butler's test suites. No Telegram, no Gemini, no API keys.

    python run_tests.py

Exits non-zero if any suite fails.

tests/test_calendar_integration.py also runs if the calendar bot repo is
checked out next to this one (or CALENDAR_BOT_DIR points at it); otherwise it
is reported as skipped. The live routing goldset (tests/live/) needs a Gemini
key and running bots, so it is never run from here.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["tests/test_router_offline.py", "tests/test_integration_stubs.py", "tests/test_web.py"]

cal_dir = Path(os.environ.get("CALENDAR_BOT_DIR", HERE.parent / "telegram-icloud-calendar-bot"))
skipped = []
if (cal_dir / "service.py").exists():
    SUITES.append("tests/test_calendar_integration.py")
else:
    skipped.append(f"tests/test_calendar_integration.py (calendar bot repo not found at {cal_dir})")

failed = []
for suite in SUITES:
    print(f"\n### {suite}", flush=True)
    env = {**os.environ, "CALENDAR_BOT_DIR": str(cal_dir)}
    if subprocess.run([sys.executable, suite], cwd=HERE, env=env).returncode != 0:
        failed.append(suite)

print("\n" + "=" * 70)
for s in skipped:
    print(f"SKIPPED: {s}")
print("FAILED: " + ", ".join(failed) if failed else f"All {len(SUITES)} suites passed.")
sys.exit(1 if failed else 0)
