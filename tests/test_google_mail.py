import base64
import hashlib
import json
import tempfile
import unittest
from email import message_from_bytes, policy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google_mail import GMAIL_SCOPE, GoogleSignIn
from mailer import build_message
from test_mailer import PDF


ORIGIN = "http://127.0.0.1:8765"
CLIENT = {"installed": {
    "client_id": "123-test.apps.googleusercontent.com", "client_secret": "fake-client-secret",
    "token_uri": "https://example.invalid/ignored",
}}


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.send_status = 200
        self.refresh_status = 200
        self.scopes = GMAIL_SCOPE

    def __call__(self, host, path, *, form=None, payload=None, token=None):
        self.calls.append((host, path, form, payload, token))
        if path == "/token":
            if form["grant_type"] == "refresh_token":
                return self.refresh_status, {"access_token": "refreshed", "expires_in": 3600, "scope": self.scopes}
            return 200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "scope": self.scopes}
        if path == "/v1/userinfo":
            return 200, {"email": "me@example.com", "email_verified": True}
        if path.endswith("/messages/send"):
            return self.send_status, {"id": "example-message"}
        raise AssertionError(path)


class GoogleMailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "google-client.json"
        self.fake = FakeGoogle()
        self.login = GoogleSignIn(self.path, request=self.fake)

    def start(self):
        self.login.configure(json.dumps(CLIENT))
        url = urlsplit(self.login.begin(ORIGIN))
        self.assertEqual(url.hostname, "accounts.google.com")
        return {key: values[0] for key, values in parse_qs(url.query).items()}

    def connect(self):
        query = self.start()
        return self.login.finish(ORIGIN, {"state": [query["state"]], "code": ["example-code"]})

    def test_oauth_checks_state_and_pkce_and_does_not_save_account_tokens(self):
        query = self.start()
        self.assertEqual(set(query["scope"].split()), {"openid", "email", GMAIL_SCOPE})
        self.assertEqual(query["code_challenge_method"], "S256")
        callback = {"state": [query["state"]], "code": ["example-code"]}
        with self.assertRaises(ValueError):
            self.login.finish(ORIGIN, {**callback, "state": ["wrong"]})
        self.assertEqual(self.fake.calls, [])
        account = self.login.finish(ORIGIN, callback)
        self.assertEqual(account.sender, "me@example.com")
        host, _, params, *_ = self.fake.calls[0]
        self.assertEqual(host, "oauth2.googleapis.com")
        self.assertEqual(params["redirect_uri"], query["redirect_uri"])
        digest = hashlib.sha256(params["code_verifier"].encode()).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).decode().rstrip("="), query["code_challenge"])
        with self.assertRaises(ValueError):
            self.login.finish(ORIGIN, callback)
        self.assertIsNone(GoogleSignIn(self.path, request=self.fake).account)

    def test_refuses_partial_consent_and_web_credentials(self):
        with self.assertRaises(ValueError):
            self.login.configure(json.dumps({"web": CLIENT["installed"]}))
        self.fake.scopes = "openid email"
        with self.assertRaisesRegex(ValueError, "permission to send"):
            self.connect()
        self.assertIsNone(self.login.account)

    def test_send_keeps_one_recipient_and_exact_pdf(self):
        account = self.connect()
        msg = build_message(account.sender, "jobs@example.com", {"subject": "Internship", "body": "Hello"}, ("Resume.pdf", PDF))
        self.assertEqual(account.deliver(msg).status, "accepted")
        host, path, _, payload, token = self.fake.calls[-1]
        self.assertEqual(host, "gmail.googleapis.com")
        self.assertEqual(path, "/gmail/v1/users/me/messages/send")
        self.assertEqual(token, "access")
        encoded = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]), policy=policy.default)
        self.assertEqual([address.addr_spec for address in encoded["To"].addresses], ["jobs@example.com"])
        attachment, = encoded.iter_attachments()
        self.assertEqual(attachment.get_filename(), "Resume.pdf")
        self.assertEqual(attachment.get_payload(decode=True), PDF)

    def test_rate_limit_pauses_and_ambiguous_server_failure_needs_review(self):
        account = self.connect()
        msg = build_message(account.sender, "jobs@example.com", {"subject": "Internship", "body": "Hello"}, ("Resume.pdf", PDF))
        self.fake.send_status = 429
        self.assertEqual(account.deliver(msg).status, "blocked")
        self.fake.send_status = 503
        self.assertEqual(account.deliver(msg).status, "review")
        self.fake.send_status = 401
        self.assertEqual(account.deliver(msg).status, "blocked")
        self.fake.refresh_status = 400
        self.assertEqual(account.deliver(msg).status, "blocked")
        self.assertFalse(account.connected)

    def test_refreshes_before_resending_after_explicit_auth_rejection(self):
        account = self.connect()
        msg = build_message(account.sender, "jobs@example.com", {"subject": "Internship", "body": "Hello"}, ("Resume.pdf", PDF))
        self.fake.send_status = 401
        self.assertEqual(account.deliver(msg).status, "blocked")
        self.fake.send_status = 200
        self.assertEqual(account.deliver(msg).status, "accepted")
        self.assertEqual(self.fake.calls[-2][2]["grant_type"], "refresh_token")
        self.assertEqual(self.fake.calls[-1][-1], "refreshed")
