"""
test_web.py — Butler's HTTP front door.

Runs against a REAL Butler instance with a fake router and registry, so the
thing being tested is the seam between HTTP and Butler's existing core —
which is where the bugs would be — rather than the routing itself, which
test_router_offline.py already covers.

The property that matters most: the app and Telegram must be ONE
conversation. A follow-up started through one must be honoured by the other.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                       # noqa: E402
import web                          # noqa: E402
from butler import Butler           # noqa: E402
from registry import Registry       # noqa: E402
from router import Router           # noqa: E402

STUB_DIR = pathlib.Path(__file__).resolve().parent.parent / "stubs"


class FakeLLM:
    """Never consulted in these tests — the fast paths cover everything here."""
    def decide(self, *a, **k):
        return {"service": None, "action": None, "params": {}, "confidence": 0.0,
                "reasoning": "fake"}
    def chat(self, message, history=None, bots=None):
        return f"(chat) {message}"


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A single allowed user, which is what the web door speaks as.
        config.ALLOWED_USER_IDS[:] = [4242]

        reg = Registry(services_file=str(STUB_DIR.parent / "services.yaml"))
        try:
            reg.load()
        except Exception:
            pass                      # no bots is fine; builtins still answer
        cls.butler = Butler(registry=reg, router=Router(reg, FakeLLM()),
                            chat_llm=FakeLLM())
        web.Handler.butler = cls.butler
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def url(self, p): return f"http://127.0.0.1:{self.port}{p}"

    def get(self, p):
        with urllib.request.urlopen(self.url(p), timeout=5) as r:
            return json.loads(r.read())

    def post(self, p, body):
        req = urllib.request.Request(self.url(p), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def chat(self, message): return self.post("/chat", {"message": message})

    def _unpatch_invoke(self):
        """
        Remove a stand-in by DELETING the instance attribute rather than
        assigning the bound method back. Assigning it back looks restored but
        leaves a shadow, and then the canary below cannot tell the difference.
        """
        vars(self.butler.registry).pop("invoke", None)

    # -- basics ------------------------------------------------------------

    def test_health(self):
        h = self.get("/health")
        self.assertTrue(h["ok"]); self.assertEqual(h["name"], "butler")
        self.assertIn("bots", h)

    def test_builtin_help_comes_back_as_text(self):
        r = self.chat("/help")
        self.assertTrue(r["ok"]); self.assertTrue(r["text"].strip())

    def test_bots_endpoint(self):
        r = self.get("/bots")
        self.assertTrue(r["ok"]); self.assertTrue(r["text"].strip())

    def test_empty_message_is_refused_politely(self):
        r = self.chat("   ")
        self.assertFalse(r["ok"]); self.assertIn("Say something", r["error"])

    def test_unknown_route_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_malformed_json_does_not_kill_the_server(self):
        req = urllib.request.Request(self.url("/chat"), data=b"{not json",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertFalse(json.loads(r.read())["ok"])
        self.assertTrue(self.get("/health")["ok"], "server died on bad input")

    # -- the shape the app relies on ---------------------------------------

    def test_buttons_are_json_objects_not_tuples(self):
        """
        Reply.buttons is a list of lists of (label, data) TUPLES. JSON has no
        tuples, and the app needs names, so they are converted. If this ever
        regresses the app renders empty buttons and the user is stuck.
        """
        self.butler.awaiting[4242] = {
            "service": "x", "action": "a", "params": {},
            "buttons": [{"label": "Add to calendar", "action": "confirm"},
                        {"label": "Cancel", "action": "cancel"}],
            "expires": 9e9,
        }
        try:
            rows = web.reply_json(
                type("R", (), {"text": "t",
                               "buttons": self.butler._followup_buttons(4242),
                               "route": None})())["buttons"]
            self.assertTrue(rows, "expected follow-up buttons")
            for row in rows:
                for b in row:
                    self.assertIn("label", b); self.assertIn("data", b)
                    self.assertIsInstance(b["label"], str)
                    self.assertIsInstance(b["data"], str)
        finally:
            self.butler.awaiting.pop(4242, None)

    def test_reply_json_is_serialisable(self):
        r = self.chat("/status")
        json.dumps(r)                       # would raise if anything leaked through

    # -- one conversation across two front doors ---------------------------

    def test_the_app_speaks_as_the_allowed_user(self):
        """
        Not an arbitrary id: the follow-up locks are keyed by user id, so if
        the web door invented its own the two front doors would be two
        separate conversations.
        """
        self.assertEqual(web.Handler._user_id(), 4242)

    # -- /invoke -----------------------------------------------------------

    def test_invoke_calls_the_bot_directly_as_butler(self):
        """
        The Home page's PC card taps a button; it should not pay for an LLM
        routing call to reach a decision the tap already made.

        The load-bearing assertion is the user id: pc-api has its own
        allowlist, and a browser-supplied id would be exactly the
        identity-spoofing anti-pattern to avoid.
        """
        seen = {}
        def fake_invoke(*, service_name, action, params, user_id, original_message):
            seen.update(service_name=service_name, action=action,
                        params=params, user_id=user_id)
            from registry import InvokeResult
            return InvokeResult(ok=True, reply="The PC is on.", data={"up": True})

        self.butler.registry.invoke = fake_invoke
        try:
            r = self.post("/invoke", {"service": "pc", "action": "pc_status"})
        finally:
            self._unpatch_invoke()

        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "The PC is on.")
        self.assertEqual(r["data"], {"up": True})
        self.assertEqual(seen["service_name"], "pc")
        self.assertEqual(seen["action"], "pc_status")
        self.assertEqual(seen["user_id"], 4242, "the browser got to pick the user id")

    def test_invoke_needs_both_a_service_and_an_action(self):
        for body in ({}, {"service": "pc"}, {"action": "pc_status"},
                     {"service": "", "action": "pc_status"}):
            with self.subTest(body=body):
                r = self.post("/invoke", body)
                self.assertFalse(r["ok"])

    def test_invoke_reports_an_unknown_bot_rather_than_crashing(self):
        r = self.post("/invoke", {"service": "nosuchbot", "action": "x"})
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_invoke_surfaces_the_bots_own_refusal(self):
        """
        pc-api answers 'You're not allowed to control this PC' for a wrong
        user id. That must reach the app as an error, not as a success with
        empty text.
        """
        from registry import InvokeResult
        self.butler.registry.invoke = lambda **k: InvokeResult(
            ok=False, error="You're not allowed to control this PC.")
        try:
            r = self.post("/invoke", {"service": "pc", "action": "pc_on_confirm"})
        finally:
            self._unpatch_invoke()
        self.assertFalse(r["ok"])
        self.assertIn("not allowed", r["error"])

    def test_invoke_does_not_leak_a_stack_trace(self):
        def boom(**k): raise RuntimeError("kaboom")
        self.butler.registry.invoke = boom
        try:
            r = self.post("/invoke", {"service": "pc", "action": "pc_status"})
        finally:
            self._unpatch_invoke()
        self.assertFalse(r["ok"])
        self.assertNotIn("Traceback", r["error"])

    def test_a_followup_held_from_telegram_is_honoured_over_http(self):
        marker = {"hit": None}

        class Held:
            ok, reply, error, data, followup = True, "continued", None, {}, None

        # Butler calls registry.invoke with keyword arguments, so the stand-in
        # has to accept them by name.
        def fake_invoke(service_name, action, params, user_id=0, original_message=""):
            marker["hit"] = (service_name, action, dict(params))
            return Held()

        # The registry MUST be restored. This test used to leave the stand-in
        # in place, and because it sorts first alphabetically, every later test
        # in this class ran against a registry that answered ok=True to
        # anything. Nothing failed, which is what made it invisible: the stub
        # happened to agree with what those tests expected. A new test that
        # asserted a FAILURE is what finally caught it.
        self.butler.registry.invoke = fake_invoke        # type: ignore[assignment]
        self.butler.awaiting[4242] = {
            "service": "calendar", "action": "amend_draft",
            "params": {"draft_id": "abc"}, "buttons": [], "expires": 9e9,
        }
        try:
            r = self.chat("make it 2pm")
            self.assertTrue(r["ok"])
            self.assertEqual(marker["hit"][0], "calendar")
            self.assertEqual(marker["hit"][1], "amend_draft")
            self.assertEqual(marker["hit"][2]["draft_id"], "abc")
        finally:
            self._unpatch_invoke()
            self.butler.awaiting.pop(4242, None)

    def test_cancel_over_http_releases_a_lock_set_elsewhere(self):
        self.butler.awaiting[4242] = {
            "service": "calendar", "action": "amend_draft", "params": {},
            "buttons": [], "expires": 9e9,
        }
        self.chat("/cancel")
        self.assertNotIn(4242, self.butler.awaiting,
                         "/cancel from the app must release a Telegram-held lock")

    def test_the_registry_is_not_left_patched_by_another_test(self):
        """
        A canary. Every test here that stands in for registry.invoke must put
        it back; one that did not poisoned the whole class for months without
        a single failure.
        """
        import registry as registry_mod
        self.assertIs(type(self.butler.registry).invoke, registry_mod.Registry.invoke)
        self.assertNotIn("invoke", vars(self.butler.registry),
                         "a test left a stand-in on the registry instance")

    # -- input validation (Gate G #14) ---------------------------------------

    BAD = {"ok": False, "error": "Bad request."}

    def raw_post(self, path, content_length, payload=b""):
        """POST with an arbitrary Content-Length header (urllib won't let us lie)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", path)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", content_length)
            conn.endheaders()
            if payload:
                conn.send(payload)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def _alive(self):
        self.assertTrue(self.get("/health")["ok"], "server died on bad input")

    def test_non_numeric_content_length_is_refused(self):
        for path in ("/chat", "/callback", "/invoke"):
            with self.subTest(path=path):
                status, body = self.raw_post(path, "abc")
                self.assertEqual((status, body), (200, self.BAD))
        self._alive()

    def test_negative_content_length_is_refused(self):
        for path in ("/chat", "/callback", "/invoke"):
            with self.subTest(path=path):
                status, body = self.raw_post(path, "-5")
                self.assertEqual((status, body), (200, self.BAD))
        self._alive()

    def test_non_object_json_is_refused(self):
        for path in ("/chat", "/callback", "/invoke"):
            for payload in (b"[]", b'"x"', b"5", b"null"):
                with self.subTest(path=path, payload=payload):
                    status, body = self.raw_post(path, str(len(payload)), payload)
                    self.assertEqual((status, body), (200, self.BAD))
        self._alive()

    def test_body_over_16kb_is_refused(self):
        big = json.dumps({"message": "a" * 20_000}).encode()
        self.assertGreater(len(big), 16_384)
        for path in ("/chat", "/callback", "/invoke"):
            with self.subTest(path=path):
                status, body = self.raw_post(path, str(len(big)), big)
                self.assertEqual((status, body), (200, self.BAD))
        self._alive()

    def test_message_over_4096_chars_is_refused(self):
        self.assertEqual(self.chat("a" * 4097), self.BAD)
        self.assertTrue(self.chat("a" * 4096)["ok"], "4096 chars is the limit, not 4095")
        self._alive()

    def test_callback_data_over_64_bytes_is_refused(self):
        self.assertEqual(self.post("/callback", {"data": "x" * 65}), self.BAD)
        self.assertEqual(self.post("/callback", {"data": "\u00e9" * 33}), self.BAD)  # 66 bytes
        self._alive()

    def test_invoke_field_limits(self):
        bad_bodies = (
            {"service": "s" * 65, "action": "x"},
            {"service": "pc", "action": "a" * 65},
            {"service": "pc", "action": "x", "params": []},
            {"service": "pc", "action": "x", "params": "text"},
            {"service": "pc", "action": "x", "message": "m" * 4097},
        )
        for body in bad_bodies:
            with self.subTest(body={k: str(v)[:12] for k, v in body.items()}):
                self.assertEqual(self.post("/invoke", body), self.BAD)
        self._alive()

    def test_internal_error_reply_has_no_exception_text(self):
        def boom(*a, **k):
            raise RuntimeError("kaboom-SECRET")
        want = {"ok": False, "error": "Butler hit an internal error."}
        self.butler.handle_message = boom
        self.butler.handle_callback = boom
        try:
            self.assertEqual(self.chat("hello"), want)
            self.assertEqual(self.post("/callback", {"data": "x"}), want)
        finally:
            vars(self.butler).pop("handle_message", None)
            vars(self.butler).pop("handle_callback", None)

    def test_invoke_internal_error_reply_has_no_exception_text(self):
        def boom(**k):
            raise RuntimeError("kaboom-SECRET")
        self.butler.registry.invoke = boom
        try:
            r = self.post("/invoke", {"service": "pc", "action": "pc_status"})
        finally:
            self._unpatch_invoke()
        self.assertEqual(r, {"ok": False, "error": "Butler hit an internal error."})

    # -- concurrency -------------------------------------------------------

    def test_parallel_requests_do_not_corrupt_butler(self):
        """
        Butler was written for one caller. Now Telegram's loop and HTTP both
        call it, so every call goes through one lock; this would surface a
        missing one.
        """
        errs, out = [], []
        def hit(i):
            try: out.append(self.chat(f"/status"))
            except Exception as e: errs.append(e)
        ts = [threading.Thread(target=hit, args=(i,)) for i in range(10)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(out), 10)
        self.assertTrue(all(r["ok"] for r in out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
