"""Mail to the parent, through the parent's own SMTP server (D-177).

"The parent is notified" (principle 3) meant a line in the audit log a parent
would read the next time they happened to open the panel. A filter that failed
at four in the afternoon was news at nine in the evening. This is the one way
BioKids reaches a parent who is not looking: an email, sent through a server
they already use and chose, listed in SECURITY.md section 4 since the start.

The settings are stored encrypted (`parent.smtp_json_encrypted`, with the key in
`/etc/biokids/secret.key`), the password included, and never shown back in
full. A message says what happened and to whom, never what a child typed or
asked: the panel holds the details, and a mail provider has no need of them.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Final

from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.config import Config

SECURITY_MODES: Final = ("starttls", "ssl", "none")
SEND_TIMEOUT: Final = 20.0


GOOGLE_HOSTS: Final = ("smtp.gmail.com", "smtp.googlemail.com")


class MailError(Exception):
    """Sending failed. The message is for the log and the panel, never a password.

    `kind` is what the panel can tell a parent (D-224): `auth` when the server
    refused the user name or password, `connect` when it could not be reached or
    spoke another kind of security, `other` for the rest.
    """

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


def tidy_password(host: str, password: str) -> str:
    """Google shows an app password in four groups of four, and a parent copies
    the spaces with it. An app password has none, so they are taken out."""
    if host.strip().lower() in GOOGLE_HOSTS:
        return "".join(password.split())
    return password


@dataclass(frozen=True, slots=True)
class MailSettings:
    host: str
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: tuple[str, ...] = field(default_factory=tuple)
    alerts: bool = True
    """Mail when something needs a parent now: a failed filter, a lock that did
    not take, a request, an assistant alert."""
    weekly: bool = True
    """A summary of the week, on Sunday evening."""

    @property
    def usable(self) -> bool:
        return bool(self.host and self.recipients and (self.sender or self.username))

    def from_address(self) -> str:
        return self.sender or self.username


def _key(config: Config) -> bytes:
    return security.load_or_create_key(config.paths.secret_key)


def load_settings(session: Session, config: Config) -> MailSettings | None:
    """The family's mail settings, or None when there are none or they cannot be
    read (a replaced key, a damaged row): no mail rather than a crash."""
    parent = repo.get_parent(session)
    if parent is None or not parent.smtp_json_encrypted:
        return None
    try:
        plain = security.decrypt_secret(parent.smtp_json_encrypted, _key(config))
    except OSError:
        return None
    if plain is None:
        return None
    try:
        raw = json.loads(plain)
        return MailSettings(
            host=str(raw.get("host", "")),
            port=int(raw.get("port", 587)),
            security=str(raw.get("security", "starttls")),
            username=str(raw.get("username", "")),
            password=str(raw.get("password", "")),
            sender=str(raw.get("sender", "")),
            recipients=tuple(str(r) for r in raw.get("recipients", []) if r),
            alerts=bool(raw.get("alerts", True)),
            weekly=bool(raw.get("weekly", True)),
        )
    except (ValueError, TypeError, AttributeError):
        return None


def save_settings(session: Session, config: Config, settings: MailSettings | None) -> None:
    parent = repo.get_parent(session)
    if parent is None:
        return
    if settings is None:
        parent.smtp_json_encrypted = None
        return
    data = asdict(settings)
    data["recipients"] = list(settings.recipients)
    parent.smtp_json_encrypted = security.encrypt_secret(json.dumps(data), _key(config))


def parse_recipients(raw: str) -> tuple[str, ...]:
    found = [part.strip() for part in raw.replace(";", ",").split(",")]
    return tuple(address for address in found if "@" in address and " " not in address)


def build_message(settings: MailSettings, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.from_address()
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="biokids.local")
    message.set_content(body)
    return message


def send(
    settings: MailSettings, subject: str, body: str, *, timeout: float = SEND_TIMEOUT
) -> None:
    """Send one plain-text message. Blocking; the daemon calls it from a thread."""
    if not settings.usable:
        raise MailError("mail is not set up")
    message = build_message(settings, subject, body)
    context = ssl.create_default_context()
    try:
        if settings.security == "ssl":
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=timeout, context=context
            ) as client:
                _deliver(client, settings, message)
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=timeout) as client:
                if settings.security == "starttls":
                    client.starttls(context=context)
                _deliver(client, settings, message)
    except (OSError, smtplib.SMTPException) as exc:
        # str(exc) from smtplib can quote the server's reply, never our password.
        raise MailError(f"{type(exc).__name__}: {exc}"[:300], failure_kind(exc)) from exc


def failure_kind(exc: BaseException) -> str:
    """`auth`, `connect` or `other`: see `MailError`."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected):
        return "connect"
    # Every SMTPException is an OSError too, so the server's other answers first.
    if isinstance(exc, smtplib.SMTPException):
        return "other"
    # An ssl.SSLError is an OSError: STARTTLS on port 465, or SSL on 587.
    if isinstance(exc, OSError):
        return "connect"
    return "other"


def _deliver(client: smtplib.SMTP, settings: MailSettings, message: EmailMessage) -> None:
    if settings.username:
        client.login(settings.username, settings.password)
    client.send_message(message)
