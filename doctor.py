"""
doctor.py — check your setup before trying to start Butler.

Run this first whenever something won't start. It checks everything Butler
needs, in the order Butler needs it, and tells you exactly what to fix.

    python doctor.py

It never contacts Telegram and never spends an API call.
"""

from __future__ import annotations

import logging
import pathlib
import sys

# Silence the modules' own logging — doctor prints its own, tidier report.
logging.disable(logging.CRITICAL)

OK, WARN, BAD = "  OK ", " WARN", " FAIL"
problems = 0


def say(status: str, message: str, fix: str = "") -> None:
    global problems
    print(f"[{status}] {message}")
    if fix:
        print(f"        -> {fix}")
    if status == BAD:
        problems += 1


print("=" * 70)
print("Butler setup check")
print("=" * 70)

# 1. Python version -----------------------------------------------------------
major, minor = sys.version_info[:2]
if (major, minor) >= (3, 10):
    say(OK, f"Python {major}.{minor}")
else:
    say(BAD, f"Python {major}.{minor} is too old",
        "Butler needs Python 3.10 or newer (it uses the `str | None` syntax).")

# 2. Dependencies -------------------------------------------------------------
for module, install in [("requests", "requests"), ("yaml", "PyYAML"),
                        ("google.genai", "google-genai")]:
    try:
        __import__(module)
        say(OK, f"{install} is installed")
    except ImportError:
        say(BAD, f"{install} is missing", f"pip install {install}")

# 3. Are we in a virtual environment? ----------------------------------------
in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
if in_venv:
    say(OK, f"virtual environment active ({pathlib.Path(sys.prefix).name})")
else:
    say(WARN, "not running inside a virtual environment",
        "Fine, but the packages went into your system Python.")

# 4. The .env file ------------------------------------------------------------
try:
    import config
except Exception as exc:  # noqa: BLE001
    say(BAD, f"config.py failed to import: {type(exc).__name__}: {exc}")
    print("\nStopping — nothing else can be checked until config.py loads.")
    raise SystemExit(1)

if config.ENV_PATH:
    say(OK, f".env found at {config.ENV_PATH}")
    found = sorted(config.ENV_KEYS)
    if found:
        say(OK, f"keys read from it: {', '.join(found)}")
    else:
        say(BAD, ".env exists but contains no KEY=VALUE lines",
            "Check it isn't empty and has no stray blank lines only.")
else:
    say(BAD, f"no .env file at {config.HERE / '.env'}",
        "cp .env.example .env    (PowerShell: Copy-Item .env.example .env)")
    for decoy in (".env.txt", "env", "env.txt"):
        if (config.HERE / decoy).exists():
            say(WARN, f"but '{decoy}' exists here",
                f"Notepad adds .txt silently. Rename-Item {decoy} .env")

# 5. The secrets themselves ---------------------------------------------------
for name, shown in config.settings.masked().items():
    if shown == "(not set)":
        say(BAD, f"{name} is not set", f"Add a line   {name}=your_value   to .env")
    else:
        say(OK, f"{name} = {shown}")

# Token shape sanity — catches a pasted-wrong token before Telegram rejects it.
token = config.settings.telegram_token
if token and ":" not in token:
    say(WARN, "BUTLER_TELEGRAM_TOKEN doesn't look like a Telegram token",
        "They look like 1234567890:AAF... — did you paste the Gemini key here?")

# 6. Who is allowed to talk to it --------------------------------------------
if config.ALLOWED_USER_IDS:
    say(OK, f"ALLOWED_USER_IDS = {config.ALLOWED_USER_IDS}")
else:
    say(BAD, "BUTLER_ALLOWED_USER_IDS is empty — Butler would ignore everyone",
        "Message @userinfobot on Telegram, then set BUTLER_ALLOWED_USER_IDS=<your id> in .env")

# 7. The address book ---------------------------------------------------------
try:
    from registry import Registry
    reg = Registry(services_file=str(config.HERE / config.SERVICES_FILE))
    reg.load()
    say(OK, f"services.yaml parsed — {len(reg.services)} service(s): "
            f"{', '.join(reg.services) or '(none)'}")
except Exception as exc:  # noqa: BLE001
    say(BAD, f"services.yaml problem: {type(exc).__name__}: {exc}")
    reg = None

# 8. Can we actually reach them? ---------------------------------------------
if reg and reg.services:
    print("\nChecking whether each bot answers...")
    reg.discover()
    for svc in reg.services.values():
        if not svc.enabled:
            say(WARN, f"{svc.name}: disabled in services.yaml")
        elif svc.online:
            say(OK, f"{svc.name}: online at {svc.url} "
                    f"({len(svc.actions)} action(s))")
        else:
            hint = ""
            if "127.0.0.1" in svc.url or "localhost" in svc.url:
                hint = (f"Start the stub:  python stubs/stub_service.py {svc.name} "
                        f"{svc.url.rsplit(':', 1)[-1]}")
            say(WARN, f"{svc.name}: not responding — {svc.last_error[:90]}", hint)

# ----------------------------------------------------------------------------
print("\n" + "=" * 70)
if problems:
    print(f"{problems} thing(s) to fix before Butler will start.")
else:
    print("Setup looks good. Start Butler with:  python main.py")
print("=" * 70)
raise SystemExit(1 if problems else 0)
