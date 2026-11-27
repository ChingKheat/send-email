"""Run the local internship email app with Python 3.10 or newer."""

import json
import os
import sqlite3
import sys
import threading
import webbrowser
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google_mail import MAX_CLIENT_FILE, GoogleSignIn
from mailer import (
    FIELDS, MAX_CONTACTS, Queue, Settings, build_message, parse_contacts, render_draft,
    run_worker, valid_address, validate_draft, validate_resume,
)


ROOT = Path(__file__).resolve().parent
STATIC_FILES = {
    "/": (ROOT / "web" / "index.html", "text/html"),
    "/app.js": (ROOT / "web" / "app.js", "text/javascript"),
    "/style.css": (ROOT / "web" / "style.css", "text/css"),
    "/sample-contacts.csv": (ROOT / "sample-contacts.csv", "text/csv"),
}
MAX_REQUEST = 25 * 1024 * 1024


class MailApp(ThreadingHTTPServer):
    def __init__(self, port, database, settings, config_error=""):
        super().__init__(("127.0.0.1", port), MailHandler)
        try:
            self.queue = Queue(database)
            self.queue.recover()
        except Exception:
            self.server_close()
            raise
        self.settings = settings
        self.config_error = config_error
        self.google = GoogleSignIn(database.with_name("google-client.json"))
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.stop = threading.Event()
        self.send_lock = threading.Lock()

    def start_worker(self):
        worker = threading.Thread(
            target=run_worker, args=(self.queue, self.active_transport, self.send_lock, self.stop), daemon=True,
        )
        worker.start()
        return worker

    def google_account(self):
        account = self.google.account
        return account if account and account.connected else None

    def active_transport(self):
        return self.google_account() if self.google.client else self.settings

    def configured(self):
        transport = self.active_transport()
        if transport is None:
            raise ValueError("Sign in with Google to send emails.")
        return transport

    @contextmanager
    def with_send_lock(self):
        if not self.send_lock.acquire(timeout=1):
            raise ValueError("A send or Google sign-in is in progress. Try again shortly.")
        try:
            yield
        finally:
            self.send_lock.release()

    def require_paused(self):
        if self.queue.status()["state"] == "running":
            raise ValueError("Pause the batch before changing your Gmail connection.")

    def preview(self, data):
        contact = data.get("contact", {})
        if not isinstance(contact, dict):
            raise ValueError("Select a contact to preview.")
        fields = {key: contact.get(key, "") for key in FIELDS if key != "greeting"}
        if any(not isinstance(value, str) or len(value) > 254 or any(ord(char) < 32 for char in value)
               for value in fields.values()):
            raise ValueError("Select a valid contact to preview.")
        return render_draft(validate_draft(data.get("draft")), fields)

    def action(self, path, data):
        queue = self.queue
        if path == "/api/import":
            return {"rows": parse_contacts(data.get("csv"))}
        if path == "/api/preview":
            return self.preview(data)
        if path == "/api/google/start":
            with self.with_send_lock():
                self.require_paused()
                return {"url": self.google.begin(self.origin)}
        if path == "/api/google/setup":
            with self.with_send_lock():
                self.require_paused()
                self.google.configure(data.get("credentials"))
            return {}
        if path == "/api/google/logout":
            with self.with_send_lock():
                self.require_paused()
                self.google.logout()
            return {}
        if path == "/api/pause":
            queue.pause()
            return {}
        if path == "/api/resume":
            with self.with_send_lock():
                queue.resume(self.configured().sender)
            return {}

        resume = validate_resume(data.get("resume"))
        draft = validate_draft(data.get("draft"))
        contacts = parse_contacts(data.get("csv")) if data.get("csv") else []
        valid = [row for row in contacts if row["status"] == "queued"]
        if path == "/api/test":
            return self.test_send(data, valid, draft, resume)
        if not valid:
            raise ValueError("Upload a CSV with at least one valid address.")
        delay = data.get("delay")
        if type(delay) is not int or not 1 <= delay <= 86_400:
            raise ValueError("Enter a delay between 1 and 86,400 seconds.")
        if path == "/api/dry-run":
            return {"count": len(valid), "skipped": len(contacts) - len(valid), "filename": resume[0]}
        count = data.get("count")
        if type(count) is not int or count != len(valid):
            raise ValueError("Confirm the number of email recipients before sending.")
        with self.with_send_lock():
            queue.start(contacts, draft, resume, delay, self.configured().sender)
        return {"count": count}

    def test_send(self, data, valid, draft, resume):
        address = data.get("to")
        if not isinstance(address, str) or not valid_address(address.strip()):
            raise ValueError("Enter your email address for the test.")
        index = data.get("index", 0)
        if type(index) is not int or (valid and not 0 <= index < len(valid)):
            raise ValueError("Select a contact to preview.")
        with self.with_send_lock():
            transport = self.configured()
            if self.queue.status()["state"] == "running":
                raise ValueError("Pause the batch before sending a test email.")
            message = build_message(transport.sender, address.strip(), render_draft(draft, valid[index] if valid else {}), resume)
            result = transport.deliver(message)
            if result.status != "accepted":
                raise ValueError(result.detail)
            return {"status": result.status}


class MailHandler(BaseHTTPRequestHandler):
    server: MailApp
    timeout = 20

    def log_message(self, *_):
        pass

    def local_host(self):
        return self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]

    def allowed(self):
        return self.local_host() and self.headers.get("Sec-Fetch-Site") in {None, "none", "same-origin"}

    def app_navigation(self, path):
        return (path == "/" and self.local_host()
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document")

    def reply(self, status, body, mime, filename=None, location=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if location:
            self.send_header("Location", location)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def json_reply(self, status, data):
        self.reply(status, json.dumps(data).encode("utf-8"), "application/json")

    def google_result(self, status, title, message):
        body = f"""<!doctype html>
        <html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{escape(title)} · Send Email</title><link rel="stylesheet" href="/style.css">
        <main class="shell"><div class="intro"><h1>{escape(title)}</h1></div>
        <section class="panel launch-help"><p>{escape(message)}</p>
        <p>Return to your Send Email tab to continue.</p><a href="/">Open Send Email</a></section></main>
        </html>"""
        return self.reply(status, body.encode("utf-8"), "text/html")

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            if url.path == "/" and url.query and self.local_host():
                query = parse_qs(url.query, max_num_fields=20)
                if {"state", "code", "error"} & query.keys():
                    # Google's cross-site callback is authorized by its single-use state.
                    try:
                        with self.server.with_send_lock():
                            self.server.require_paused()
                            self.server.google.finish(self.server.origin, query)
                    except ValueError as exc:
                        return self.google_result(400, "Could not connect Gmail", str(exc))
                    return self.google_result(200, "Gmail connected", "You can now send a test email from your Gmail account.")
            if not self.allowed() and not self.app_navigation(url.path):
                if url.path == "/oauth/google/start" and self.local_host():
                    return self.google_result(403, "Open Send Email first", "Start Google sign-in from the main Send Email page.")
                return self.json_reply(403, {"error": "Open the app at its local address."})
            if url.path == "/oauth/google/start":
                try:
                    location = self.server.action("/api/google/start", {})["url"]
                except ValueError as exc:
                    return self.google_result(400, "Could not start Google sign-in", str(exc))
                return self.reply(303, b"", "text/html", location=location)
            if url.path in STATIC_FILES:
                file, mime = STATIC_FILES[url.path]
                name = file.name if mime == "text/csv" else None
                return self.reply(200, file.read_bytes(), mime, name)
            if url.path == "/api/status":
                page = int(parse_qs(url.query).get("page", ["0"])[0])
                if not 0 <= page <= MAX_CONTACTS // 10:
                    raise ValueError("Choose a valid results page.")
                data = self.server.queue.status(page)
                account = self.server.google_account()
                google_client = self.server.google.client
                data.update(
                    configured=self.server.active_transport() is not None,
                    google_ready=google_client is not None,
                    google_project=google_client.project_id if google_client else "",
                    google_sender=account.sender if account else "",
                    google_error=self.server.google.error,
                    config_error=self.server.config_error,
                )
                return self.json_reply(200, data)
            if url.path == "/api/results":
                results = self.server.queue.export().encode("utf-8-sig")
                return self.reply(200, results, "text/csv", "send-results.csv")
            return self.json_reply(404, {"error": "Page not found."})
        except ValueError as exc:
            return self.json_reply(400, {"error": str(exc)})
        except sqlite3.Error:
            return self.json_reply(503, {"error": "The local queue is unavailable. Try again shortly."})

    def do_POST(self):
        if not self.allowed() or self.headers.get_all("Origin") != [self.server.origin]:
            return self.json_reply(403, {"error": "Open the app at its local address."})
        if self.path not in {"/api/import", "/api/preview", "/api/dry-run", "/api/test", "/api/start", "/api/pause",
                             "/api/resume", "/api/google/setup", "/api/google/start", "/api/google/logout"}:
            return self.json_reply(404, {"error": "Action not found."})
        if self.headers.get_content_type() != "application/json":
            return self.json_reply(415, {"error": "Expected a JSON request."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            limit = MAX_CLIENT_FILE * 2 if self.path == "/api/google/setup" else MAX_REQUEST
            if not 1 <= size <= limit:
                return self.json_reply(413, {"error": "The request is empty or too large."})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return self.json_reply(200, self.server.action(self.path, data))
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            return self.json_reply(400, {"error": "The request could not be read."})
        except ValueError as exc:
            return self.json_reply(400, {"error": str(exc)})
        except sqlite3.Error:
            return self.json_reply(503, {"error": "The local queue is unavailable. Try again shortly."})


def main():
    data = Path(os.environ.get("SEND_EMAIL_DATA_DIR")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Send Email")
    config_error = ""
    try:
        settings = Settings.from_environment(data / ".env")
    except (OSError, ValueError) as exc:
        settings = None
        config_error = "Check the .env file format and SMTP values." if isinstance(exc, UnicodeError) else (
            str(exc) if isinstance(exc, ValueError) else "Could not read .env."
        )
    try:
        data.mkdir(parents=True, exist_ok=True)
        with MailApp(8765, data / "queue.db", settings, config_error) as app:
            worker = app.start_worker()
            print(f"Send Email is open at {app.origin}", flush=True)
            print("Keep this window open while sending. Press Ctrl+C to close the app.", flush=True)
            if "--no-browser" not in sys.argv:
                webbrowser.open(app.origin)
            try:
                app.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                app.stop.set()
                if worker.is_alive():
                    print("Finishing any email already in progress…", flush=True)
                    worker.join()
    except OSError as exc:
        print(f"Could not start Send Email. Check whether another copy is open. ({exc})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
