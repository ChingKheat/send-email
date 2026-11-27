import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app import MailApp
from test_google_mail import CLIENT, FakeGoogle
from test_mailer import PDF


@contextmanager
def local_app(database, google):
    with MailApp(0, database, None) as app:
        app.google.request = google
        server = threading.Thread(target=app.serve_forever, daemon=True)
        server.start()
        worker = app.start_worker()
        try:
            yield app
        finally:
            app.stop.set()
            worker.join(3)
            app.shutdown()
            server.join(3)


class GoogleWorkflowTests(unittest.TestCase):
    def test_browser_connection_and_queue_use_mock_gmail(self):
        with tempfile.TemporaryDirectory() as tmp, local_app(Path(tmp) / "queue.db", FakeGoogle()) as app:
            def call(path, data=None, site="same-origin", navigation=False):
                headers = {"Sec-Fetch-Site": site}
                if navigation:
                    headers.update({"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
                if data is not None:
                    headers.update({"Content-Type": "application/json", "Origin": app.origin})
                connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=4)
                try:
                    connection.request("GET" if data is None else "POST", path,
                                       body=json.dumps(data).encode() if data is not None else None, headers=headers)
                    response = connection.getresponse()
                    return response.status, response.getheader("Location"), response.read()
                finally:
                    connection.close()

            page = call("/")[2]
            self.assertIn(b"Sign in with Google", page)
            self.assertIn(b"Set up Gmail", page)
            self.assertIn(b"https://console.cloud.google.com/auth/overview/personal?clientType=desktop", page)
            self.assertEqual(call("/", site="cross-site", navigation=True)[0], 200)
            self.assertEqual(call("/", site="cross-site")[0], 403)
            self.assertEqual(call("/api/status", site="cross-site", navigation=True)[0], 403)
            self.assertEqual(call("/api/google/setup", {"credentials": json.dumps(CLIENT)}, site="cross-site")[0], 403)
            credentials = {"installed": {**CLIENT["installed"], "project_id": "send-email-example"}}
            self.assertEqual(call("/api/google/setup", {"credentials": json.dumps(credentials)})[0], 200)
            self.assertEqual(json.loads(call("/api/status")[2])["google_project"], "send-email-example")
            self.assertEqual(call("/oauth/google/start", site="cross-site")[0], 403)
            self.assertEqual(call("/api/google/start", {}, site="cross-site")[0], 403)
            response, _, result = call("/api/google/start", {})
            self.assertEqual(response, 200)
            location = json.loads(result)["url"]
            authorize = parse_qs(urlsplit(location).query)
            self.assertEqual(authorize["redirect_uri"], [app.origin + "/"])
            self.assertEqual(call("/?code=fake&state=wrong", site="cross-site")[0], 400)
            self.assertEqual(app.google.request.calls, [])
            query = urlencode({"state": authorize["state"][0], "code": "fake"})
            status, _, result = call("/?" + query, site="cross-site")
            self.assertEqual(status, 200)
            self.assertIn(b"Gmail connected", result)
            self.assertEqual(call("/?" + query, site="cross-site")[0], 400)
            profile = json.loads(call("/api/status")[2])
            self.assertEqual(profile["google_sender"], "me@example.com")
            self.assertTrue(profile["configured"])
            self.assertNotIn("refresh", json.dumps(profile))

            payload = {
                "csv": "email,company\njobs@example.com,Example\n", "delay": 1, "count": 1,
                "draft": {"subject": "Internship inquiry", "body": "Hi {{greeting}}"},
                "resume": {"name": "Resume.pdf", "data": base64.b64encode(PDF).decode()},
            }
            self.assertEqual(call("/api/test", {**payload, "to": "me@example.com"})[0], 200)
            self.assertEqual(call("/api/start", payload)[0], 200)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                state = json.loads(call("/api/status")[2])
                if state["state"] == "complete":
                    break
                time.sleep(0.05)
            self.assertEqual(state["counts"], {"accepted": 1})
            sends = [entry for entry in app.google.request.calls if entry[0] == "gmail.googleapis.com"]
            self.assertEqual(len(sends), 2)

            status, _, result = call("/api/google/start", {})
            self.assertEqual(status, 200)
            pending = parse_qs(urlsplit(json.loads(result)["url"]).query)["state"][0]
            self.assertEqual(call("/api/google/logout", {}, site="cross-site")[0], 403)
            self.assertEqual(call("/api/google/logout", {})[0], 200)
            self.assertEqual(call("/api/google/logout", {})[0], 200)
            profile = json.loads(call("/api/status")[2])
            self.assertEqual(profile["google_sender"], "")
            self.assertTrue(profile["google_ready"])
            self.assertFalse(profile["configured"])
            self.assertEqual(profile["counts"], {"accepted": 1})
            self.assertEqual(call("/?" + urlencode({"state": pending, "code": "fake"}), site="cross-site")[0], 400)
            self.assertEqual(call("/api/test", {**payload, "to": "me@example.com"})[0], 400)
            status, _, result = call("/api/google/start", {})
            self.assertEqual(status, 200)
            state = parse_qs(urlsplit(json.loads(result)["url"]).query)["state"][0]
            self.assertEqual(call("/?" + urlencode({"state": state, "code": "fake"}), site="cross-site")[0], 200)
            self.assertIn(b'id="google-logout"', page)
