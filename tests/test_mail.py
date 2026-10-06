"""D-177 — mail to the parent, through their own server."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy.orm import Session

from biokids_core import mail, repo
from biokids_core.config import Config
from biokids_core.db import AiAlert, Child, Family, LedgerDay, Parent
from biokids_daemon.notifier import Notifier

SUNDAY_EVENING = dt.datetime(2026, 10, 11, 19, 30)
MONDAY = dt.datetime(2026, 10, 12, 9, 0)


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def __call__(self, _settings: mail.MailSettings, subject: str, body: str, **_: Any) -> None:
        if self.fail:
            raise mail.MailError("ConnectionRefusedError: [Errno 111]")
        self.sent.append((subject, body))


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(mail, "send", box)
    return box


@pytest.fixture
def family(session: Session, config: Config) -> Child:
    session.add(Family(locale="tr"))
    session.add(Parent(display_name="Anne"))
    child = Child(unix_user="ahmet", display_name="Ahmet", birth_year=2021)
    session.add(child)
    session.flush()
    mail.save_settings(
        session,
        config,
        mail.MailSettings(
            host="smtp.example.com",
            username="anne@example.com",
            password="s3cret-app-password",
            recipients=("anne@example.com",),
        ),
    )
    session.commit()
    return child


def test_the_settings_are_stored_encrypted(
    session: Session, config: Config, family: Child
) -> None:
    parent = repo.get_parent(session)
    assert parent is not None and parent.smtp_json_encrypted
    assert "s3cret" not in parent.smtp_json_encrypted
    loaded = mail.load_settings(session, config)
    assert loaded is not None and loaded.password == "s3cret-app-password"


def test_recipients_are_read_forgivingly() -> None:
    assert mail.parse_recipients("a@x.com; b@y.org , not an address") == ("a@x.com", "b@y.org")


async def test_turning_mail_on_does_not_send_the_familys_history(
    session: Session, config: Config, family: Child, outbox: Outbox
) -> None:
    repo.audit(session, actor="daemon", action="filter_failed", severity="alert")
    session.commit()
    notifier = Notifier(config)
    assert not await notifier.run(session, MONDAY, 0.0)
    assert outbox.sent == []


async def test_what_needs_a_parent_is_mailed_once_and_says_who(
    session: Session, config: Config, family: Child, outbox: Outbox
) -> None:
    notifier = Notifier(config)
    await notifier.run(session, MONDAY, 0.0)

    repo.audit(
        session,
        actor="daemon",
        action="lock_failed",
        target=f"child:{family.id}",
        severity="alert",
    )
    repo.audit(
        session, actor="child:ahmet", action="access_requested", target=f"child:{family.id}"
    )
    repo.audit(session, actor="parent:1", action="policy_changed")  # not mailed
    session.add(AiAlert(child_id=family.id, score=90, category="self_harm", text="the words"))
    session.commit()

    assert await notifier.run(session, MONDAY, 60.0)
    assert await notifier.run(session, MONDAY, 120.0) is False, "mailed twice"
    [(subject, body)] = outbox.sent
    assert "3" in subject
    assert body.count("Ahmet") == 3
    assert "the words" not in body, "what a child typed never goes in a mail"


async def test_a_mail_that_fails_is_sent_later(
    session: Session, config: Config, family: Child, outbox: Outbox
) -> None:
    notifier = Notifier(config)
    await notifier.run(session, MONDAY, 0.0)
    repo.audit(session, actor="daemon", action="filter_down", severity="alert")
    session.commit()

    outbox.fail = True
    assert not await notifier.run(session, MONDAY, 60.0)
    outbox.fail = False
    assert not await notifier.run(session, MONDAY, 90.0), "the backoff was not respected"
    assert await notifier.run(session, MONDAY, 200.0)
    assert len(outbox.sent) == 1


async def test_the_week_is_summed_up_once_on_sunday_evening(
    session: Session, config: Config, family: Child, outbox: Outbox
) -> None:
    session.add(LedgerDay(child_id=family.id, day=dt.date(2026, 10, 7), general_s=45 * 60))
    session.add(LedgerDay(child_id=family.id, day=dt.date(2026, 10, 11), general_s=30 * 60))
    session.commit()
    notifier = Notifier(config)
    await notifier.run(session, SUNDAY_EVENING - dt.timedelta(hours=2), 0.0)
    assert outbox.sent == [], "not before seven"

    await notifier.run(session, SUNDAY_EVENING, 60.0)
    await notifier.run(session, SUNDAY_EVENING + dt.timedelta(minutes=5), 120.0)
    weekly = [body for subject, body in outbox.sent if "özet" in subject]
    assert len(weekly) == 1
    assert "Ahmet" in weekly[0] and "75 dk" in weekly[0]
    assert "Çarşamba: 45 dk" in weekly[0]
