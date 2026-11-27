import email
import email.policy
import tempfile
import threading
import unittest
from pathlib import Path
from socketserver import StreamRequestHandler, TCPServer

from mailer import (
    Delivery, Queue, Settings, build_message, parse_contacts, render_draft,
    send_email, validate_draft, validate_resume,
)


PDF = b"%PDF-1.4\nExample resume\n%%EOF"


class MockSMTP(StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost Test SMTP\r\n")
        recipients = []
        while line := self.rfile.readline():
            command = line.split(maxsplit=1)[0].upper()
            if command in {b"EHLO", b"HELO", b"MAIL"}:
                self.wfile.write(b"250 OK\r\n")
            elif command == b"RCPT":
                recipients.append(line.strip())
                self.wfile.write(b"250 OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 Continue\r\n")
                lines = []
                while (line := self.rfile.readline()) not in {b".\r\n", b""}:
                    lines.append(line[1:] if line.startswith(b"..") else line)
                self.server.received.append((recipients, b"".join(lines)))
                self.wfile.write(b"250 Accepted\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.contacts = "email,company,contact_name\nhr@example.com,Acme,Ana\njobs@example.com,Example,\n"
        self.draft = validate_draft({"subject": "Internship at {{company}}", "body": "Hi {{greeting}},\nI'm Mei."})
        self.resume = ("Mei – Resume.pdf", PDF)

    def test_import_lists_invalid_and_duplicate_rows(self):
        contacts = parse_contacts("\ufeffEmail,Company,Contact_Name\nhr@example.com,\"A, Inc.\",Ana\nHR@example.com,Other,\noops,,\n")
        self.assertEqual([row["status"] for row in contacts], ["queued", "skipped", "skipped"])
        self.assertEqual(contacts[0]["company"], "A, Inc.")
        self.assertEqual(contacts[2]["row"], 4)
        self.assertEqual(render_draft(self.draft, contacts[0])["body"], "Hi Ana,\nI'm Mei.")

    def test_missing_personalization_has_a_fallback(self):
        contact = parse_contacts("email\njobs@example.com\n")[0]
        self.assertEqual(render_draft(self.draft, contact), {
            "subject": "Internship at your company", "body": "Hi Hiring Team,\nI'm Mei.",
        })

    def test_bad_csv_draft_and_resume_are_rejected(self):
        for call in (
            lambda: parse_contacts("company\nAcme\n"),
            lambda: parse_contacts('email,company\nhr@example.com,"Acme\n'),
            lambda: validate_draft({"subject": "Hello\nBcc: x@example.com", "body": "Hi"}),
            lambda: validate_draft({"subject": "Hello {{unknown}}", "body": "Hi"}),
            lambda: validate_resume({"name": "resume.pdf", "data": "dGV4dA=="}),
        ):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_two_mock_sends_each_have_one_recipient_and_pdf(self):
        with TCPServer(("127.0.0.1", 0), MockSMTP) as smtp:
            smtp.received = []
            thread = threading.Thread(target=smtp.serve_forever, daemon=True)
            thread.start()
            try:
                config = Settings("127.0.0.1", smtp.server_address[1], "mock", "me@example.com")
                for contact in parse_contacts(self.contacts):
                    draft = render_draft(self.draft, contact)
                    message = build_message(config.sender, contact["email"], draft, self.resume)
                    self.assertEqual(send_email(config, message).status, "accepted")
            finally:
                smtp.shutdown()

        self.assertEqual(len(smtp.received), 2)
        for expected, (envelope, raw) in zip(("hr@example.com", "jobs@example.com"), smtp.received):
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            self.assertEqual(envelope, [f"rcpt TO:<{expected}>".encode()])
            self.assertEqual([address.addr_spec for address in msg["To"].addresses], [expected])
            attachments = list(msg.iter_attachments())
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].get_content_type(), "application/pdf")
            self.assertEqual(attachments[0].get_filename(), self.resume[0])
            self.assertEqual(attachments[0].get_payload(decode=True), PDF)

    def test_pause_restart_and_uncertain_delivery_never_resend_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "queue.db"
            queue = Queue(db)
            queue.start(parse_contacts(self.contacts + "third@example.com,,\n"), self.draft, self.resume, 2)
            first = queue.claim(100)
            queue.finish(first, Delivery("accepted", ""), 101)
            self.assertIsNone(queue.claim(102))
            second = queue.claim(103)
            self.assertIsNotNone(second)

            restarted = Queue(db)
            restarted.recover()
            self.assertEqual(restarted.status()["state"], "paused")
            self.assertIsNone(restarted.claim(200))
            restarted.resume()
            third = restarted.claim(200)
            self.assertEqual(third["email"], "third@example.com")
            restarted.finish(third, Delivery("accepted", ""), 201)
            self.assertEqual(restarted.status()["counts"]["accepted"], 2)
            self.assertEqual(restarted.status()["counts"]["review"], 1)

    def test_temporary_failure_retries_without_blocking_other_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(Path(tmp) / "queue.db")
            queue.start(parse_contacts(self.contacts), self.draft, self.resume, 2)
            first = queue.claim(100)
            queue.finish(first, Delivery("retry", "SMTP 451"), 101)
            second = queue.claim(103)
            self.assertEqual(second["email"], "jobs@example.com")
            queue.finish(second, Delivery("accepted", ""), 104)
            self.assertIsNone(queue.claim(130))
            self.assertEqual(queue.claim(131)["email"], "hr@example.com")


if __name__ == "__main__":
    unittest.main()
