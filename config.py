"""
config.py — every tunable value in one place.

Nothing in this file is secret. Secrets live in .env (see settings() at the bottom).
"""

import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# Reading .env
#
# Worth understanding, because it bit us on day one: `.env` is not a Python
# thing and not an operating-system thing. It is just a text file. Docker
# Compose happens to read it (that's the `env_file:` line in
# docker-compose.yml) and copies its contents into the container's environment.
#
# Plain `python main.py` does NO such thing. os.getenv() reads the environment
# your shell actually has, and your shell has never heard of that file. So on
# the server it worked and in PowerShell it said "GEMINI_API_KEY is not set".
#
# Rather than add a fourth dependency (python-dotenv) for fifteen lines of
# work, Butler reads the file itself. Real environment variables still win, so
# Docker's behaviour is unchanged.
# ----------------------------------------------------------------------------

def load_env_file(path: pathlib.Path | None = None) -> tuple[dict[str, str], pathlib.Path | None]:
    """Return (keys found, the file we read). Real env vars are never overwritten."""
    target = path or (HERE / ".env")
    if not target.exists():
        return {}, None

    found: dict[str, str] = {}
    # utf-8-sig strips the invisible BOM that Windows Notepad likes to add.
    # Without this, the first key comes out named "﻿BUTLER_TELEGRAM_TOKEN"
    # and silently never matches.
    for raw_line in target.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):]

        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()

        # Strip matching surrounding quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key:
            found[key] = value
            os.environ.setdefault(key, value)   # a real env var beats the file

    return found, target


ENV_KEYS, ENV_PATH = load_env_file()

# ----------------------------------------------------------------------------
# EDITABLE — plain values, no code. Change, rebuild, done.
# ----------------------------------------------------------------------------

# Which Telegram user IDs may talk to Butler. Everyone else is ignored silently.
# Set BUTLER_ALLOWED_USER_IDS in .env (comma-separated). Find yours by messaging
# @userinfobot on Telegram. Empty means Butler refuses to start.
ALLOWED_USER_IDS = [
    int(x) for x in os.getenv("BUTLER_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
]

# The model that makes routing decisions. Same family as the calendar bot.
ROUTER_MODEL = "gemini-3.5-flash-lite"

# How sure the router must be before it acts without asking.
# 0.0 = act on anything, 1.0 = always ask. 0.65 is a sensible starting point.
# This catches "I don't really know" — a model unsure of every option.
ROUTER_CONFIDENCE_THRESHOLD = 0.65

# How far ahead the winning bot must be of the runner-up before Butler acts.
# THIS is the knob for genuinely ambiguous messages, and usually the one to
# reach for first.
#
# Why it exists: on 2026-09-07 the router sent "remind me about the CPF
# contribution deadline" straight to calendar at 0.80 confidence. I wanted to
# be asked. But 0.80 is high, so no confidence threshold below 0.80 would have
# caught it — and anything above 0.80 would make Butler ask about nearly
# everything. One number cannot distinguish "sure" from "sure, but the other
# bot was almost as good".
#
# So the router now asks the model to score a second choice too:
#   calendar 0.80 / finance 0.75  -> margin 0.05 -> a coin flip, ask
#   calendar 0.80 / finance 0.05  -> margin 0.75 -> clearly calendar, act
#
# Raise this to be asked more often on overlapping messages; lower it to be
# asked less. 0.0 disables margin checking entirely.
ROUTER_MARGIN_THRESHOLD = 0.25

# Bots exempt from the time-guard (router.py): a keyword hit + a time/recurrence
# phrase skips the keyword path and goes to the LLM UNLESS the bot is listed here.
ROUTER_TIME_GUARD_EXEMPT = [
    s.strip() for s in os.getenv("ROUTER_TIME_GUARD_EXEMPT", "calendar").split(",") if s.strip()
]

# How long Butler waits for a bot to answer /invoke before giving up.
INVOKE_TIMEOUT_SECONDS = 25

# How long Butler waits for /health and /capabilities.
DISCOVERY_TIMEOUT_SECONDS = 5

# How often Butler re-checks which bots are alive (seconds).
HEALTH_REFRESH_SECONDS = 300

# Telegram long-poll wait. Higher = fewer requests, slower Ctrl-C. 25 is fine.
POLL_TIMEOUT_SECONDS = 25

# Butler's own personality for the small-talk path (when no bot fits).
BUTLER_PERSONA = (
    "You are Butler, a personal assistant running on a home server. "
    "You are concise and factual. You never invent information. "
    "If you do not know something, say so plainly. "
    "Keep replies under 4 sentences unless asked for detail."
)

# Butler's HTTP front door, used by the PWA. It listens only on the internal
# Docker network — no ports are published — so the only route in is nginx,
# behind an access-controlled reverse proxy (Cloudflare Access in my setup). Set WEB_ENABLED False to turn the app off entirely
# and leave Butler as a Telegram-only bot.
WEB_ENABLED = os.getenv("BUTLER_WEB_ENABLED", "1") not in ("0", "false", "False", "")
WEB_HOST = os.getenv("BUTLER_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("BUTLER_WEB_PORT", "8080"))

# Set True to print every routing decision to the log with its reasoning.
# Leave True until you trust the router.
VERBOSE_ROUTING = True


# ----------------------------------------------------------------------------
# NOT EDITABLE without understanding the code below this line.
# ----------------------------------------------------------------------------

SERVICES_FILE = os.getenv("BUTLER_SERVICES_FILE", "services.yaml")

# Resolve relative to this file, not the shell's current directory — so
# `python tests/doctor.py` from anywhere still finds the address book.
if not os.path.isabs(SERVICES_FILE):
    SERVICES_FILE = str(HERE / SERVICES_FILE)


class Settings:
    """Secrets, read from the environment at startup. Never hard-code these."""

    def __init__(self) -> None:
        self.telegram_token = os.getenv("BUTLER_TELEGRAM_TOKEN", "")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")

    def where_env_came_from(self) -> list[str]:
        """
        Diagnostics printed on a failed start. The point is to answer
        "why didn't it see my file?" without you having to guess.
        """
        lines = []
        if ENV_PATH:
            names = ", ".join(sorted(ENV_KEYS)) or "(none — the file has no KEY=VALUE lines)"
            lines.append(f"Read .env from: {ENV_PATH}")
            lines.append(f"Keys found in it: {names}")
        else:
            lines.append(f"No .env file at: {HERE / '.env'}")
            # The classic Windows trap: Notepad appends .txt without telling you,
            # and Explorer hides the extension so the file *looks* correct.
            for decoy in (".env.txt", ".env.example", "env"):
                if (HERE / decoy).exists():
                    lines.append(
                        f"  ...but I can see '{decoy}' there. If that's meant to be "
                        f"your .env, rename it:  Rename-Item {decoy} .env"
                    )
        return lines

    def validate(self) -> list[str]:
        """Return a list of human-readable problems. Empty list means good to go."""
        problems = []
        if not self.telegram_token:
            problems.append(
                "BUTLER_TELEGRAM_TOKEN is not set. Create a bot with @BotFather "
                "and put the token in your .env file."
            )
        if not self.gemini_api_key:
            problems.append(
                "GEMINI_API_KEY is not set. Get one from https://aistudio.google.com/apikey "
                "and put it in your .env file."
            )
        if not ALLOWED_USER_IDS:
            problems.append(
                "BUTLER_ALLOWED_USER_IDS is not set, so Butler would ignore everyone. "
                "Message @userinfobot on Telegram to get your ID, then add it to .env."
            )
        return problems

    def masked(self) -> dict[str, str]:
        """For logging. Never print a secret in full — not even to your own terminal."""
        def mask(v: str) -> str:
            if not v:
                return "(not set)"
            return f"{v[:4]}…{v[-4:]} ({len(v)} chars)"
        return {
            "BUTLER_TELEGRAM_TOKEN": mask(self.telegram_token),
            "GEMINI_API_KEY": mask(self.gemini_api_key),
        }


settings = Settings()
