"""Explicit Gmail SMTP and SendGrid digest delivery adapters."""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from agent.digest import Digest


class EmailDeliveryError(RuntimeError):
    """Raised when configured digest delivery cannot complete."""


class EmailSender(Protocol):
    async def send(self, digest: Digest, recipient: str) -> None: ...


class GmailSmtpSender:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host or os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com")
        self.port = port or int(os.getenv("GMAIL_SMTP_PORT", "587"))
        self.username = username or os.getenv("GMAIL_USERNAME")
        self.password = password or os.getenv("GMAIL_APP_PASSWORD")

    async def send(self, digest: Digest, recipient: str) -> None:
        _require_value(self.username, "GMAIL_USERNAME")
        _require_value(self.password, "GMAIL_APP_PASSWORD")
        message = EmailMessage()
        message["Subject"] = digest.subject
        message["From"] = self.username
        message["To"] = recipient
        message.set_content(digest.text)
        message.add_alternative(digest.html, subtype="html")
        try:
            await asyncio.to_thread(self._send_message, message)
        except (OSError, smtplib.SMTPException) as error:
            raise EmailDeliveryError(f"Gmail SMTP delivery failed: {error}") from error

    def _send_message(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=30) as client:
            client.starttls()
            client.login(self.username, self.password)
            client.send_message(message)


class SendGridSender:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_email: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("SENDGRID_API_KEY")
        self.from_email = from_email or os.getenv("SENDGRID_FROM_EMAIL")
        self.transport = transport

    async def send(self, digest: Digest, recipient: str) -> None:
        _require_value(self.api_key, "SENDGRID_API_KEY")
        _require_value(self.from_email, "SENDGRID_FROM_EMAIL")
        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": self.from_email},
            "subject": digest.subject,
            "content": [
                {"type": "text/plain", "value": digest.text},
                {"type": "text/html", "value": digest.html},
            ],
        }
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise EmailDeliveryError(f"SendGrid delivery failed: {error}") from error


def create_email_sender(name: str | None = None) -> EmailSender:
    provider = (name or os.getenv("EMAIL_PROVIDER", "gmail")).casefold()
    if provider == "gmail":
        return GmailSmtpSender()
    if provider == "sendgrid":
        return SendGridSender()
    raise ValueError("EMAIL_PROVIDER must be 'gmail' or 'sendgrid'.")


def _require_value(value: str | None, name: str) -> None:
    if not value or value.startswith("replace_with_"):
        raise EmailDeliveryError(f"{name} is not configured.")
