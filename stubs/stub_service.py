"""
stub_service.py — fake bots that implement the Butler contract.

Nothing here is production code. Its whole purpose is to let you run Butler
end-to-end on your laptop with no server, no iCloud, and no real bots.

It is also the shortest possible reference implementation of CONTRACT.md — when
you write a real bot's adapter, copy the shape of this file.

Run:
    python stubs/stub_service.py calendar 9101
    python stubs/stub_service.py finance  9102
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- The two fake bots ------------------------------------------------------

PROFILES = {
    "calendar": {
        "version": "stub-1.0",
        "capabilities": {
            "name": "calendar",
            "description": (
                "Creates and reads calendar events on the user's iCloud calendars from "
                "natural language. Handles anything with a date or time attached. "
                "Does NOT track money, habits, or news."
            ),
            "keywords": ["calendar", "event", "meeting", "appointment",
                         "schedule", "reschedule", "book"],
            "actions": [
                {
                    "name": "create_event",
                    "description": "Create a calendar event from a natural-language description.",
                    "params": {"text": "The user's original message, verbatim."},
                }
            ],
        },
    },
    "finance": {
        "version": "stub-1.0",
        "capabilities": {
            "name": "finance",
            "description": (
                "Tracks the user's money: investments, CPF, savings, insurance policies "
                "and loans. Answers questions about balances and contributions. "
                "Does NOT schedule anything or create calendar events."
            ),
            "keywords": ["cpf", "investment", "savings", "insurance",
                         "loan", "portfolio", "dividend", "premium"],
            "actions": [
                {
                    "name": "log_transaction",
                    "description": "Record a transaction, contribution or premium payment.",
                    "params": {"text": "The user's original message, verbatim."},
                },
                {
                    "name": "query_balance",
                    "description": "Answer a question about a current balance or holding.",
                    "params": {"question": "What the user wants to know."},
                },
            ],
        },
    },
}


class Handler(BaseHTTPRequestHandler):
    profile_name = "calendar"

    # -- plumbing ------------------------------------------------------------

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # quieter test output
        sys.stderr.write(f"[{self.profile_name}] {fmt % args}\n")

    # -- the three contract endpoints ----------------------------------------

    def do_GET(self):  # noqa: N802 — name fixed by http.server
        profile = PROFILES[self.profile_name]

        if self.path == "/health":
            return self._json(200, {
                "ok": True,
                "name": self.profile_name,
                "version": profile["version"],
            })

        if self.path == "/capabilities":
            return self._json(200, profile["capabilities"])

        return self._json(404, {"ok": False, "error": "no such endpoint"})

    def do_POST(self):  # noqa: N802
        if self.path != "/invoke":
            return self._json(404, {"ok": False, "error": "no such endpoint"})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(200, {"ok": False, "error": "invalid JSON in request"})

        action = body.get("action")
        params = body.get("params") or {}
        original = body.get("original_message", "")
        rid = body.get("request_id", "?")

        known = {a["name"] for a in PROFILES[self.profile_name]["capabilities"]["actions"]}
        if action not in known:
            return self._json(200, {"ok": False, "error": f"unknown action '{action}'"})

        # A deliberate failure path so you can see how Butler reports a bot saying no.
        if "explode" in original.lower():
            return self._json(200, {"ok": False, "error": "stub was asked to fail on purpose"})

        arg = params.get("text") or params.get("question") or original
        return self._json(200, {
            "ok": True,
            "reply": f"*{self.profile_name} stub* handled `{action}`\n\n> {arg}",
            "data": {"request_id": rid, "echo": params},
        })


def serve(profile_name: str, port: int, host: str = "127.0.0.1") -> None:
    if profile_name not in PROFILES:
        raise SystemExit(f"unknown stub '{profile_name}'; choose from {list(PROFILES)}")

    handler = type("BoundHandler", (Handler,), {"profile_name": profile_name})
    server = HTTPServer((host, port), handler)
    print(f"stub '{profile_name}' listening on http://{host}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    # host defaults to 127.0.0.1 (laptop: reachable only by you).
    # In Docker it must be 0.0.0.0, otherwise the container only listens to
    # itself and Butler's connection is refused. Compose passes it explicitly.
    name = sys.argv[1] if len(sys.argv) > 1 else "calendar"
    portno = int(sys.argv[2]) if len(sys.argv) > 2 else 9101
    bind = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    serve(name, portno, bind)
