"""
web.py — Butler's HTTP front door, for the PWA.

WHY THIS IS A THREAD AND NOT A SECOND CONTAINER
-----------------------------------------------
islam-bot runs its Telegram bot and its HTTP adapter as two separate
processes, because everything they share lives in a file (the corpus) or in
SQLite (the round). Butler is different: `awaiting`, `pending` and `history`
are plain dicts on the Butler object, in memory. A second process would get
its own copy, and then:

  - a draft you started in Telegram would be invisible to the app
  - a follow-up lock held by one would be ignored by the other
  - "make it 2pm" would route as if it were a fresh message

So this runs in the SAME process, against the SAME Butler instance. The
pleasant consequence is that the two front doors are genuinely one
conversation: start something in Telegram, finish it in the app.

Butler was never written to be re-entrant, and now two things call it, so
every call goes through one lock.

Auth: none here, deliberately. This listens only on the internal Docker
network with no published ports; the only route in is nginx behind an access
layer (Cloudflare Access in my deployment). Adding a second, weaker auth scheme here would be theatre.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import config

log = logging.getLogger("butler.web")

# Input limits (Gate G #14). Telegram's own limits: 4096 chars per message,
# 64 bytes per callback_data.
MAX_BODY = 16_384
MAX_MESSAGE = 4096
MAX_DATA_BYTES = 64
MAX_NAME = 64
_DRAIN_LIMIT = 65_536           # unread body we will swallow so the client sees our reply
BAD_REQUEST = "Bad request."
INTERNAL_ERROR = "Butler hit an internal error."


class BadRequest(Exception):
    """Raised for malformed input. Detail never reaches the client."""

# One lock for all of Butler. Telegram's loop and an HTTP request must never be
# inside handle_message() at the same time — they mutate the same dicts.
_lock = threading.Lock()


def reply_json(reply: Any) -> dict:
    """A Reply, as the app needs it."""
    buttons = [
        [{"label": label, "data": data} for (label, data) in row]
        for row in (reply.buttons or [])
    ]
    out: dict = {"ok": True, "text": reply.text, "buttons": buttons}
    if reply.route is not None:
        r = reply.route
        out["route"] = {
            "kind": getattr(r, "kind", None),
            "service": getattr(r, "service", None),
            "method": getattr(r, "method", None),
            "confidence": getattr(r, "confidence", None),
            "reasoning": getattr(r, "reasoning", None),
        }
    return out


class Handler(BaseHTTPRequestHandler):
    butler = None

    def log_message(self, *_args):
        pass                    # Butler's own logger already records each route

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        """Parse the JSON object body, or raise BadRequest (never returns a non-dict)."""
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise BadRequest()
        if length < 0:
            raise BadRequest()
        if length > MAX_BODY:
            if length <= _DRAIN_LIMIT:
                self.rfile.read(length)
            raise BadRequest()
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):     # JSONDecodeError + bad UTF-8
            raise BadRequest()
        if not isinstance(body, dict):
            raise BadRequest()
        return body

    @staticmethod
    def _user_id() -> int:
        """
        The app always speaks as the owner — the same user id the Telegram side uses,
        which is what makes the two front doors one conversation. There is no
        user to choose: ALLOWED_USER_IDS is the whole world of this bot.
        """
        return config.ALLOWED_USER_IDS[0] if config.ALLOWED_USER_IDS else 0

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "name": "butler",
                                    "bots": sorted(s.name for s in
                                                   self.butler.registry.online_services())})
        if self.path == "/bots":
            with _lock:
                return self._send(200, {"ok": True, "text": self.butler.bots_summary()})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/invoke":
            return self._do_invoke()
        if self.path not in ("/chat", "/callback"):
            return self._send(404, {"ok": False, "error": "not found"})

        user_id = self._user_id()

        try:
            body = self._body()
            if self.path == "/chat":
                message = str(body.get("message") or "").strip()
                if not message:
                    return self._send(200, {"ok": False, "error": "Say something."})
                if len(message) > MAX_MESSAGE:
                    raise BadRequest()
            else:
                data = str(body.get("data") or "")
                if not data:
                    return self._send(200, {"ok": False, "error": "No button data."})
                if len(data.encode("utf-8")) > MAX_DATA_BYTES:
                    raise BadRequest()
            with _lock:
                if self.path == "/chat":
                    reply = self.butler.handle_message(user_id, message)
                else:
                    reply = self.butler.handle_callback(user_id, data)
        except BadRequest:
            return self._send(200, {"ok": False, "error": BAD_REQUEST})
        except Exception:                              # never leak a stack trace
            log.exception("web request failed")
            return self._send(200, {"ok": False, "error": INTERNAL_ERROR})

        return self._send(200, reply_json(reply))

    # -- deterministic invoke ---------------------------------------------

    def _do_invoke(self):
        """
        POST /invoke {service, action, params} — skip the router, call a bot
        directly.

        WHY THIS EXISTS AND WHY IT IS NOT A WIDENING
        --------------------------------------------
        The app's Home page has a PC card with real buttons. Getting there via
        /chat would mean sending the words "turn on my PC" and paying for an
        LLM routing call to reach a decision that was already made by the tap.
        Slow, and occasionally wrong.

        It grants nothing new: /chat can already reach every action on every
        service in Butler's address book — that is what routing IS. This only
        removes the guessing.

        Crucially, the USER ID IS STILL BUTLER'S. The browser does not get to
        say who it is; pc-api has its own allowlist and it is checked against
        the same id the Telegram side uses. A browser-supplied user_id would
        be exactly the identity-spoofing anti-pattern to avoid.
        """
        try:
            body = self._body()
            service = str(body.get("service") or "").strip()
            action = str(body.get("action") or "").strip()
            params = body.get("params")
            if params is None:
                params = {}
            original = str(body.get("message") or "")
            if not service or not action:
                return self._send(200, {"ok": False,
                                        "error": "Needs a service and an action."})
            if (len(service) > MAX_NAME or len(action) > MAX_NAME
                    or not isinstance(params, dict) or len(original) > MAX_MESSAGE):
                raise BadRequest()
            with _lock:
                res = self.butler.registry.invoke(
                    service_name=service, action=action, params=params,
                    user_id=self._user_id(),
                    original_message=original)
        except BadRequest:
            return self._send(200, {"ok": False, "error": BAD_REQUEST})
        except Exception:
            log.exception("invoke failed")
            return self._send(200, {"ok": False, "error": INTERNAL_ERROR})

        if not res.ok:
            return self._send(200, {"ok": False, "error": res.error or "That failed."})
        return self._send(200, {"ok": True, "reply": res.reply,
                                "data": res.data or {}})


def serve(butler, host: str = "0.0.0.0", port: int = 8080) -> threading.Thread:
    """Start the HTTP front door on a daemon thread and return it."""
    Handler.butler = butler
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="butler-web")
    thread.start()
    log.info("HTTP front door on %s:%d (/health /bots /chat /callback /invoke)",
             host, port)
    return thread
