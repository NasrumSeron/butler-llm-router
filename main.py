"""
main.py — starts Butler and runs the polling loop forever.

Startup order matters and is deliberate:
  1. Validate secrets      — fail loudly now, not at 2am on the first message
  2. Load services.yaml    — fail loudly if the address book is malformed
  3. Discover bots         — log who answered; a dead bot is a warning, not fatal
  4. Poll Telegram         — forever
"""

from __future__ import annotations

import logging
import sys
import time

import config
from butler import Butler
from llm import GeminiLLM
from registry import Registry
from router import Router
from telegram_io import Telegram
import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("butler.main")


def build() -> tuple[Telegram, Butler]:
    problems = config.settings.validate()
    if problems:
        log.error("Butler cannot start:")
        for p in problems:
            log.error("  • %s", p)
        log.error("")
        for line in config.settings.where_env_came_from():
            log.error("  %s", line)
        sys.exit(1)

    for name, value in config.settings.masked().items():
        log.info("%s = %s", name, value)

    registry = Registry()
    registry.load()
    registry.discover()

    brain = GeminiLLM(api_key=config.settings.gemini_api_key)
    router = Router(registry=registry, llm=brain)
    butler = Butler(registry=registry, router=router, chat_llm=brain)

    tg = Telegram(config.settings.telegram_token)
    me = tg.get_me()
    log.info("Connected to Telegram as @%s", me.get("username"))

    # The PWA's front door. Same process, same Butler instance, so a
    # conversation started in Telegram can be finished in the app and the
    # follow-up locks are shared rather than duplicated. See web.py.
    if config.WEB_ENABLED:
        web.serve(butler, config.WEB_HOST, config.WEB_PORT)

    return tg, butler


def run(tg: Telegram, butler: Butler) -> None:
    offset: int | None = None
    log.info("Butler is listening. Ctrl-C to stop.")

    while True:
        try:
            updates = tg.get_updates(offset)
        except Exception as exc:  # noqa: BLE001 — a network blip must not kill the bot
            log.warning("getUpdates failed (%s); retrying in 5s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                dispatch(tg, butler, update)
            except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
                log.exception("failed handling update %s: %s", update.get("update_id"), exc)


def dispatch(tg: Telegram, butler: Butler, update: dict) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        user_id = cq["from"]["id"]
        chat_id = cq["message"]["chat"]["id"]
        tg.answer_callback(cq["id"])
        if not allowed(user_id):
            return
        reply = butler.handle_callback(user_id, cq.get("data", ""))
        tg.send(chat_id, reply.text, reply.buttons or None)
        return

    message = update.get("message")
    if not message or "text" not in message:
        return

    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]

    if not allowed(user_id):
        log.warning("ignored message from unauthorised user %s", user_id)
        return

    reply = butler.handle_message(user_id, message["text"])
    tg.send(chat_id, reply.text, reply.buttons or None)


def allowed(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


if __name__ == "__main__":
    telegram, the_butler = build()
    try:
        run(telegram, the_butler)
    except KeyboardInterrupt:
        log.info("Stopped.")
