"""Email via SMTP (send) and IMAP (receive)."""
from __future__ import annotations

import email
import imaplib
import os
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


class EmailConnector:
    channel = "email"

    def __init__(self) -> None:
        self.smtp_host = os.environ.get("SMTP_HOST", "")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587") or 587)
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", self.smtp_user)
        self.imap_host = os.environ.get("IMAP_HOST", "")
        self.imap_user = os.environ.get("IMAP_USER", self.smtp_user)
        self.imap_password = os.environ.get("IMAP_PASSWORD", self.smtp_password)

    @property
    def can_send(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    @property
    def can_receive(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def send(self, to_addr: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to_addr
        msg["Subject"] = subject or "(без темы)"
        msg.set_content(body)
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.smtp_user, self.smtp_password)
            smtp.send_message(msg)
        return msg.get("Message-ID", "")

    def fetch_unseen(self, limit: int = 20) -> list[dict]:
        out: list[dict] = []
        with imaplib.IMAP4_SSL(self.imap_host) as imap:
            imap.login(self.imap_user, self.imap_password)
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:limit]:
                _, msg_data = imap.fetch(num, "(RFC822)")
                raw = msg_data[0][1] if msg_data and msg_data[0] else None
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                name, addr = parseaddr(_decode(msg.get("From")))
                out.append({
                    "channel": "email",
                    "from_addr": addr,
                    "from_name": name,
                    "subject": _decode(msg.get("Subject")),
                    "body": _plain_body(msg)[:20000],
                    "external_id": msg.get("Message-ID") or f"imap-{num.decode()}",
                })
        return out
