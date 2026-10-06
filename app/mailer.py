"""Outbound email, used only for profile email-verification codes.

SMTP via the standard library: no provider SDK, so any mailbox that offers
SMTP works -- a Gmail app password, Amazon SES, Brevo, a self-hosted relay.
The portal has never sent email before this, so every setting is optional
and `configured()` tells callers whether sending is possible at all; the
profile page explains the situation to the user instead of failing.
"""

import smtplib
import ssl
from email.message import EmailMessage

from app import config


class MailError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.SMTP_HOST and config.MAIL_FROM)


def send(*, to: str, subject: str, body: str) -> None:
    """Send one plain-text message. Raises MailError on any failure."""
    if not configured():
        raise MailError("SMTP is not configured (SMTP_HOST and MAIL_FROM are required).")
    message = EmailMessage()
    message["From"] = config.MAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        if config.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(
                config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS)
        with server:
            if not config.SMTP_USE_SSL and config.SMTP_USE_TLS:
                server.starttls(context=ssl.create_default_context())
            if config.SMTP_USERNAME:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Could not send email via {config.SMTP_HOST}: {exc}") from exc
