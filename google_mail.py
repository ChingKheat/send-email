"""Authorize the local desktop app and send messages through the Gmail API."""

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from mailer import Delivery, valid_address


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_HOST = "oauth2.googleapis.com"
MAX_CLIENT_FILE = 16_384
NETWORK_ERRORS = (OSError, http.client.HTTPException)


def google_request(host, path, *, form=None, payload=None, token=None):
    headers, body = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(form).encode("ascii")
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("ascii")
    connection = http.client.HTTPSConnection(host, timeout=10)
    try:
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        raw = response.read(65_536)
        try:
            data = json.loads(raw)
        except (UnicodeError, ValueError, RecursionError):
            data = {}
        return response.status, data if isinstance(data, dict) else {}
    finally:
        connection.close()


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str = field(repr=False)
    project_id: str = ""

    @classmethod
    def from_file(cls, raw):
        error = "Choose the JSON credentials for a Google OAuth Desktop app."
        if not isinstance(raw, str) or len(raw) > MAX_CLIENT_FILE:
            raise ValueError(error)
        try:
            values = json.loads(raw)
            desktop = values["installed"]
            client_id, secret = desktop["client_id"], desktop["client_secret"]
            project_id = desktop.get("project_id", "")
            if (isinstance(client_id, str) and isinstance(secret, str)
                    and re.fullmatch(r"[A-Za-z0-9-]{1,200}\.apps\.googleusercontent\.com", client_id)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", secret)):
                if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
                    project_id = ""
                return cls(client_id, secret, project_id)
        except (ValueError, TypeError, KeyError, RecursionError):
            pass
        raise ValueError(error)

    def exchange(self, request, **grant):
        return request(TOKEN_HOST, "/token", form={
            "client_id": self.client_id, "client_secret": self.client_secret, **grant,
        })


class GoogleSignIn:
    """The server serializes changes to the client and account with the send lock."""

    def __init__(self, path, request=google_request):
        self.path = path
        self.request = request
        self.client = None
        self.account = None
        self.pending = None
        self.error = ""
        try:
            with path.open(encoding="utf-8-sig") as source:
                self.client = Client.from_file(source.read(MAX_CLIENT_FILE + 1))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "The saved Google credentials could not be read. Import the Desktop app JSON again."

    def configure(self, raw):
        client = Client.from_file(raw)
        values = {"installed": {"client_id": client.client_id, "client_secret": client.client_secret, "project_id": client.project_id}}
        source = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                source = file.name
                json.dump(values, file)
            os.replace(source, self.path)
        except OSError as exc:
            raise ValueError("Could not save the Google setup file on this computer.") from exc
        finally:
            if source:
                try:
                    os.unlink(source)
                except OSError:
                    pass
        self.client, self.pending, self.account, self.error = client, None, None, ""

    def logout(self):
        self.account = None
        self.pending = None

    def begin(self, origin):
        if self.client is None:
            raise ValueError("Import the Google Desktop app credentials first.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        self.pending = (state, verifier, time.monotonic() + 600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.client.client_id, "redirect_uri": origin + "/", "response_type": "code",
            "scope": f"openid email {GMAIL_SCOPE}", "code_challenge_method": "S256",
            "code_challenge": challenge.decode("ascii").rstrip("="), "state": state,
            "access_type": "offline", "prompt": "consent select_account",
        })

    def finish(self, origin, query):
        state = query.get("state", [])
        if (not self.pending or len(state) != 1 or not state[0].isascii()
                or not secrets.compare_digest(self.pending[0], state[0]) or self.pending[2] <= time.monotonic()):
            raise ValueError("This sign-in attempt expired or could not be verified. Try again from Send Email.")
        _, verifier, _ = self.pending
        self.pending = None
        code = query.get("code", [])
        if "error" in query or len(code) != 1:
            raise ValueError("Google sign-in was not completed. Return to Send Email to try again.")
        try:
            status, tokens = self.client.exchange(
                self.request, grant_type="authorization_code", code=code[0],
                redirect_uri=origin + "/", code_verifier=verifier,
            )
            if status != 200:
                raise ValueError("Google could not complete sign-in. Check the Google setup and try again.")
            if GMAIL_SCOPE not in granted_scopes(tokens):
                raise ValueError("Allow permission to send email when Google asks, then try again.")
            access, refresh = token_value(tokens, "access_token"), token_value(tokens, "refresh_token")
            if not access or not refresh:
                raise ValueError("Google did not return the required authorization. Try signing in again.")
            status, profile = self.request("openidconnect.googleapis.com", "/v1/userinfo", token=access)
        except NETWORK_ERRORS as exc:
            raise ValueError("Could not reach Google. Check your internet connection and try again.") from exc
        sender = profile.get("email")
        if status != 200 or profile.get("email_verified") is not True or not isinstance(sender, str) or not valid_address(sender):
            raise ValueError("Google could not verify the address for this account.")
        self.account = GoogleAccount(sender, self.client, self.request, refresh, access, expires_at(tokens))
        return self.account


def token_value(tokens, field):
    value = tokens.get(field)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", value) else ""


def granted_scopes(tokens):
    scopes = tokens.get("scope", "")
    return scopes.split() if isinstance(scopes, str) else []


def expires_at(tokens):
    seconds = tokens.get("expires_in")
    return time.monotonic() + max(0, seconds - 60) if type(seconds) is int else time.monotonic()


@dataclass
class GoogleAccount:
    sender: str
    client: Client
    request: object
    refresh_token: str = field(repr=False)
    access_token: str = field(repr=False)
    expiry: float

    @property
    def connected(self):
        return bool(self.refresh_token)

    def token(self):
        if self.access_token and self.expiry > time.monotonic():
            return self.access_token
        if not self.connected:
            return ""
        status, tokens = self.client.exchange(self.request, grant_type="refresh_token", refresh_token=self.refresh_token)
        if status in {400, 401, 403} or (status == 200 and "scope" in tokens and GMAIL_SCOPE not in granted_scopes(tokens)):
            self.refresh_token, self.access_token = "", ""
            return ""
        if status != 200:
            raise OSError("Google token refresh is temporarily unavailable.")
        self.access_token = token_value(tokens, "access_token")
        self.refresh_token = token_value(tokens, "refresh_token") or self.refresh_token
        self.expiry = expires_at(tokens)
        return self.access_token

    def deliver(self, message):
        try:
            token = self.token()
        except NETWORK_ERRORS:
            return Delivery("blocked", "Could not refresh the Gmail connection. Resume when Google is reachable.")
        if not token:
            return Delivery("blocked", "Your Gmail connection expired. Sign in with Google again, then resume.")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            status, _ = self.request("gmail.googleapis.com", "/gmail/v1/users/me/messages/send",
                                    payload={"raw": raw}, token=token)
        except NETWORK_ERRORS:
            return Delivery("review", "The connection to Gmail ended before the send could be confirmed.")
        if 200 <= status < 300:
            return Delivery("accepted", "")
        if status == 401:
            self.access_token = ""
            return Delivery("blocked", "Gmail rejected the current connection. Resume to refresh it or sign in again.")
        if status == 429:
            return Delivery("blocked", "Gmail reached a sending limit. Wait before resuming this batch.")
        if status == 403:
            return Delivery("blocked", "Gmail blocked this send. Check Gmail API access and sending limits before resuming.")
        if status >= 500 or status in {408, 409}:
            return Delivery("review", f"Gmail did not confirm the send (HTTP {status}).")
        return Delivery("failed", f"Gmail rejected the message (HTTP {status}).")
