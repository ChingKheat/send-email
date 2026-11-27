import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from socketserver import TCPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import MailApp
from mailer import Settings
from test_mailer import MockSMTP, PDF


class WorkflowTests(unittest.TestCase):
    def test_http_workflow_import_preview_dry_run_and_send(self):
        with tempfile.TemporaryDirectory() as tmp, TCPServer(("127.0.0.1", 0), MockSMTP) as smtp:
            smtp.received = []
            settings = Settings("127.0.0.1", smtp.server_address[1], "mock", "me@example.com")
            with MailApp(0, Path(tmp) / "queue.db", settings) as app:
                threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (smtp, app)]
                for thread in threads:
                    thread.start()
                worker = app.start_worker()
                try:
                    origin = f"http://127.0.0.1:{app.server_port}"

                    def call(path, data=None, request_origin=origin):
                        body = None if data is None else json.dumps(data).encode()
                        request = Request(origin + path, data=body, headers={
                            "Origin": request_origin, "Content-Type": "application/json",
                        })
                        with urlopen(request, timeout=5) as response:
                            return response.read()

                    self.assertIn(b"Internship outreach", call("/"))
                    raw = "email,company,contact_name\nhr@example.com,Example,Ana\nHR@example.com,Example,\n"
                    imported = json.loads(call("/api/import", {"csv": raw}))
                    self.assertEqual(imported["rows"][1]["detail"], "Duplicate email address")
                    payload = {
                        "csv": raw, "index": 0,
                        "draft": {"subject": "Internship at {{company}}", "body": "Hi {{greeting}},\nI'm Mei."},
                        "resume": {"name": "Resume.pdf", "data": base64.b64encode(PDF).decode()},
                        "delay": 1,
                    }
                    preview = json.loads(call("/api/preview", {
                        "contact": imported["rows"][0], "draft": payload["draft"],
                    }))
                    self.assertEqual(preview["body"], "Hi Ana,\nI'm Mei.")
                    with self.assertRaises(HTTPError) as cross_origin:
                        call("/api/start", {**payload, "count": 1}, "http://example.com")
                    self.assertEqual(cross_origin.exception.code, 403)
                    with self.assertRaises(HTTPError):
                        call("/api/start", {**payload, "count": 2})
                    check = json.loads(call("/api/dry-run", payload))
                    self.assertEqual(check, {"count": 1, "skipped": 1, "filename": "Resume.pdf"})
                    self.assertEqual(smtp.received, [])
                    send_test = json.loads(call("/api/test", {**payload, "to": "me@example.com"}))
                    self.assertEqual(send_test["status"], "accepted")
                    call("/api/start", {**payload, "count": 1})
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        state = json.loads(call("/api/status"))
                        if state["state"] == "complete":
                            break
                        time.sleep(0.05)
                    self.assertEqual(state["counts"], {"accepted": 1, "skipped": 1})
                    self.assertEqual([entry[0] for entry in smtp.received], [
                        [b"rcpt TO:<me@example.com>"], [b"rcpt TO:<hr@example.com>"],
                    ])
                    self.assertIn(b"hr@example.com,Example,Ana,accepted", call("/api/results"))
                finally:
                    app.stop.set()
                    worker.join(3)
                    app.shutdown()
                    smtp.shutdown()
