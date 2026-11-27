"""Contact import, message creation, SMTP delivery, and the durable send queue."""

import base64
import binascii
import csv
import io
import os
import re
import smtplib
import sqlite3
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path


MAX_CSV = 5 * 1024 * 1024
MAX_PDF = 10 * 1024 * 1024
MAX_CONTACTS = 50_000
ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
TOKEN = re.compile(r"{{\s*([^{}]+?)\s*}}")
FIELDS = {"email": "example@example.com", "company": "your company", "contact_name": "Hiring Team", "greeting": "Hiring Team"}


def valid_address(address):
    return len(address) <= 254 and len(address.partition("@")[0]) <= 64 and ADDRESS.fullmatch(address) is not None


def parse_contacts(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CSV:
        raise ValueError("Choose a CSV file up to 5 MB.")
    reader = csv.reader(io.StringIO(raw.removeprefix("\ufeff"), newline=""), strict=True)
    try:
        header = [name.strip().lower() for name in next(reader, [])]
        if header.count("email") != 1 or any(header.count(key) > 1 for key in ("company", "contact_name")):
            raise ValueError("Use a CSV with one email column and optional company and contact_name columns.")
        columns = {key: header.index(key) for key in ("email", "company", "contact_name") if key in header}
        contacts = []
        seen = set()
        for row in reader:
            if not row or not any(value.strip() for value in row):
                continue
            if len(contacts) >= MAX_CONTACTS:
                raise ValueError("Use a CSV with up to 50,000 contacts.")
            fields = {
                key: row[index].strip() if index is not None and index < len(row) else ""
                for key, index in ((key, columns.get(key)) for key in ("email", "company", "contact_name"))
            }
            address = fields["email"]
            normalized = address.lower()
            if len(row) != len(header):
                issue = "Wrong number of columns"
            elif not valid_address(address):
                issue = "Invalid email address"
            elif normalized in seen:
                issue = "Duplicate email address"
            elif any(len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value)
                     for value in (fields["company"], fields["contact_name"])):
                issue = "Company or contact name must fit on one line and be up to 200 characters"
            else:
                issue = ""
                seen.add(normalized)
            contacts.append({
                **fields, "email": address[:254], "company": fields["company"][:200],
                "contact_name": fields["contact_name"][:200], "row": reader.line_num,
                "status": "skipped" if issue else "queued", "detail": issue,
            })
    except csv.Error as exc:
        raise ValueError(f"The CSV could not be read near line {reader.line_num}.") from exc
    if not contacts:
        raise ValueError("Add at least one contact to the CSV.")
    return contacts


def validate_draft(data):
    if not isinstance(data, dict):
        raise ValueError("Enter a subject and message.")
    draft = {}
    for key, label, limit in (("subject", "Subject", 240), ("body", "Message", 20_000)):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{label} must contain 1 to {limit:,} characters.")
        if key == "subject" and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Keep the subject on one line.")
        for token in TOKEN.findall(value):
            if token not in FIELDS:
                raise ValueError("Use only the personalization fields shown below the message.")
        draft[key] = value.strip()
    return draft


def render_draft(draft, contact):
    fields = {key: contact.get(key) or default for key, default in FIELDS.items()}
    fields["greeting"] = fields["contact_name"]
    return {key: TOKEN.sub(lambda match: fields[match[1]], draft[key]) for key in ("subject", "body")}


def validate_resume(data):
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("data"), str):
        raise ValueError("Attach your résumé as a PDF.")
    name = data["name"]
    if (not name.lower().endswith(".pdf") or len(name) > 200
            or any(char in "\\/" or ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("Choose a PDF with a valid filename.")
    if len(data["data"]) > 4 * ((MAX_PDF + 2) // 3):
        raise ValueError("Choose a PDF up to 10 MB.")
    try:
        content = base64.b64decode(data["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("The résumé could not be read.") from exc
    if len(content) > MAX_PDF or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
        raise ValueError("Choose a valid PDF up to 10 MB.")
    return name, content


def build_message(sender, recipient, draft, resume):
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = draft["subject"]
    message["Date"] = formatdate()
    message["Message-ID"] = make_msgid(domain=sender.partition("@")[2])
    message.set_content(draft["body"], cte="quoted-printable")
    message.add_attachment(resume[1], maintype="application", subtype="pdf", filename=resume[0])
    return message


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    security: str
    sender: str
    username: str = ""
    password: str = ""

    @classmethod
    def from_environment(cls, env_path):
        values = {}
        if env_path.exists():
            for number, line in enumerate(env_path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if not separator or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key.strip()):
                    raise ValueError(f"Invalid SMTP settings on line {number} of .env.")
                values[key.strip()] = value.strip()
        values.update(os.environ)
        host = values.get("SMTP_HOST", "")
        if not host:
            return None
        security = values.get("SMTP_SECURITY", "starttls").lower()
        if security not in {"starttls", "ssl"}:
            raise ValueError("SMTP_SECURITY must be starttls or ssl.")
        try:
            port = int(values.get("SMTP_PORT", "465" if security == "ssl" else "587"))
        except ValueError as exc:
            raise ValueError("Enter a numeric SMTP_PORT between 1 and 65535.") from exc
        sender = values.get("SMTP_FROM", "")
        username, password = values.get("SMTP_USERNAME", ""), values.get("SMTP_PASSWORD", "")
        if not 1 <= port <= 65535 or not valid_address(sender) or bool(username) != bool(password):
            raise ValueError("Check SMTP_PORT, SMTP_FROM, and the SMTP username and password in .env.")
        return cls(host, port, security, sender, username, password)

    def deliver(self, message):
        return send_email(self, message)


@dataclass(frozen=True)
class Delivery:
    status: str
    detail: str


def send_email(config, message):
    client = None
    data_started = False
    try:
        if config.security == "ssl":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=15, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=15)
            if config.security == "starttls":
                client.starttls(context=ssl.create_default_context())
        client.ehlo_or_helo_if_needed()
        if config.username:
            client.login(config.username, config.password)
        code, reply = client.mail(config.sender)
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, reply, config.sender)
        code, reply = client.rcpt(str(message["To"]))
        if code not in {250, 251}:
            raise smtplib.SMTPResponseException(code, reply)
        message_bytes = message.as_bytes()
        data_started = True
        code, reply = client.data(message_bytes)
        if code != 250:
            raise smtplib.SMTPResponseException(code, reply)
        return Delivery("accepted", "")
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError):
        return Delivery("blocked", "Check SMTP sign-in and encryption settings, then resume.")
    except smtplib.SMTPSenderRefused as exc:
        if 400 <= exc.smtp_code < 500:
            return Delivery("retry", f"Temporary SMTP response ({exc.smtp_code}).")
        return Delivery("blocked", f"The provider rejected the sender (SMTP {exc.smtp_code}). Check SMTP_FROM.")
    except smtplib.SMTPResponseException as exc:
        status = "retry" if 400 <= exc.smtp_code < 500 else "failed"
        return Delivery(status, f"SMTP response {exc.smtp_code}.")
    except OSError:
        if data_started:
            return Delivery("review", "The connection ended before the provider confirmed receipt.")
        return Delivery("retry", "Could not connect to the SMTP provider.")
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass


class Queue:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaign (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    filename TEXT NOT NULL, pdf BLOB NOT NULL, delay INTEGER NOT NULL,
                    next_send REAL NOT NULL DEFAULT 0, notice TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY, row INTEGER NOT NULL, email TEXT NOT NULL,
                    company TEXT NOT NULL, contact_name TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    ready_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS contact_queue ON contacts(status, ready_at, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE contacts SET status='review', detail=? WHERE status='sending'",
                       ("The app closed before it recorded the provider’s response.",))
            db.execute("UPDATE campaign SET state='paused', notice=? WHERE state='running'",
                       ("The app restarted. Resume when you’re ready.",))
            self.complete_if_finished(db)

    def start(self, contacts, draft, resume, delay, sender=""):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT state, next_send FROM campaign").fetchone()
            if saved and saved["state"] != "complete":
                raise ValueError("Finish the current batch before starting another one.")
            db.execute("DELETE FROM contacts")
            db.execute("DELETE FROM campaign")
            db.execute("""INSERT INTO campaign (id, state, subject, body, filename, pdf, delay, next_send, sender)
                          VALUES (1, 'running', ?, ?, ?, ?, ?, ?, ?)""",
                       (draft["subject"], draft["body"], *resume, delay, saved["next_send"] if saved else 0, sender))
            db.executemany("""INSERT INTO contacts (row, email, company, contact_name, status, detail)
                              VALUES (:row, :email, :company, :contact_name, :status, :detail)""", contacts)

    def pause(self):
        with self.connect() as db:
            db.execute("UPDATE campaign SET state='paused', notice='' WHERE state='running'")

    def resume(self, sender=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT sender, state FROM campaign").fetchone()
            if saved and saved["state"] == "paused":
                if sender is not None and saved["sender"] != sender:
                    raise ValueError("Connect the original sender address before resuming this batch.")
                db.execute("UPDATE campaign SET state='running', notice=''")

    def claim(self, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT 1 FROM campaign WHERE state='running' AND next_send <= ?", (now,)).fetchone()
            if not active or db.execute("SELECT 1 FROM contacts WHERE status='sending'").fetchone():
                return None
            row = db.execute("""SELECT * FROM contacts WHERE status='queued' AND ready_at <= ?
                                ORDER BY ready_at, id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            batch = db.execute("SELECT subject, body, filename, pdf, delay FROM campaign").fetchone()
            db.execute("UPDATE contacts SET status='sending', attempts=attempts+1 WHERE id=?", (row["id"],))
            return {**dict(batch), **dict(row), "attempts": row["attempts"] + 1}

    def finish(self, item, delivery, now):
        with self.connect() as db:
            state, ready = delivery.status, 0
            attempts = item["attempts"]
            if state == "retry":
                if attempts < 3:
                    state, ready = "queued", now + max(item["delay"], 30 * 2 ** (attempts - 1))
                else:
                    state = "failed"
            elif state == "blocked":
                state, attempts = "queued", attempts - 1
                db.execute("UPDATE campaign SET state='paused', notice=?", (delivery.detail,))
            db.execute("UPDATE campaign SET next_send=?", (now + item["delay"],))
            db.execute("""UPDATE contacts SET status=?, detail=?, ready_at=?, attempts=? WHERE id=?""",
                       (state, delivery.detail, ready, attempts, item["id"]))
            self.complete_if_finished(db)

    @staticmethod
    def complete_if_finished(db):
        if not db.execute("SELECT 1 FROM contacts WHERE status IN ('queued', 'sending')").fetchone():
            db.execute("UPDATE campaign SET state='complete', notice=''")

    def status(self, page=0):
        with self.connect() as db:
            db.execute("BEGIN")
            batch = db.execute("SELECT state, notice, filename, delay FROM campaign").fetchone()
            counts = dict(db.execute("SELECT status, COUNT(*) FROM contacts GROUP BY status"))
            rows = db.execute("""SELECT row, email, status, detail FROM contacts
                                 ORDER BY id LIMIT 10 OFFSET ?""", (page * 10,))
            return {
                "state": batch["state"] if batch else "empty",
                "notice": batch["notice"] if batch else "",
                "filename": batch["filename"] if batch else "",
                "delay": batch["delay"] if batch else None,
                "counts": counts, "rows": [dict(row) for row in rows],
            }

    def export(self):
        with self.connect() as db:
            rows = db.execute("SELECT row, email, company, contact_name, status, detail, attempts FROM contacts ORDER BY id")
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([column[0] for column in rows.description])
            for row in rows:
                writer.writerow([csv_cell(value) for value in row])
            return output.getvalue()


def csv_cell(value):
    if isinstance(value, str) and value and (value[0] in "\t\r\n" or value.lstrip()[:1] in {"=", "+", "-", "@"}):
        return "'" + value
    return value


def run_worker(queue, active_transport, send_lock, stop):
    while not stop.is_set():
        with send_lock:
            transport = active_transport()
            item = queue.claim(time.time()) if transport and not stop.is_set() else None
            if item is not None:
                draft = render_draft(item, item)
                message = build_message(transport.sender, item["email"], draft, (item["filename"], item["pdf"]))
                queue.finish(item, transport.deliver(message), time.time())
        if item is None:
            stop.wait(0.35)
