"""What the daemon mails to the parent, and when (D-177).

Once a minute, from the supervisor's loop:

* **Alerts**, batched into one message: every audit row of severity `alert`
  since the last mail - a filter that failed, a lock that did not take, the
  daemon giving up - every request a child filed, and every assistant alert.
  Only what is new: the first run starts from the present, so turning mail on
  does not send a family its history.
* **The week**, on Sunday from 19:00: each child's time per day, and the top
  three highlights of the insights screen (D-206).

A mail that cannot be sent is tried again later, with a growing pause, and
nothing is marked sent until it was (principle 5: offline is a queue, not a
loss). The send runs in a thread with its own timeout; the loop never waits on
an SMTP server for longer than that.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core import insights, mail, repo
from biokids_core.config import Config
from biokids_core.db.models import AiAlert, AuditLog, Child, LedgerDay
from biokids_core.i18n import Translator, normalise_locale, translator

log = logging.getLogger(__name__)

LAST_AUDIT: Final = "mail.last_audit_id"
LAST_WEEK: Final = "mail.last_weekly"
MAILED_ACTIONS: Final = frozenset({"access_requested", "parent_added", "parent_removed"})
"""Not alerts, but a parent wants them at once: a child is waiting for an answer,
or somebody can now sign in to the panel who could not before - or the other
way round (D-218)."""
BACKOFF: Final = (60, 300, 900, 3600)
MAX_ROWS: Final = 40
WEEKLY_HOUR: Final = 19
MAIL_HIGHLIGHTS: Final = 3
"""Per child in the Sunday mail: the rest is on the insights screen."""


@dataclass
class Notifier:
    config: Config
    failures: int = 0
    next_attempt: float = 0.0

    async def run(self, session: Session, now_wall: dt.datetime, now_boot: float) -> bool:
        """Send what is due. True when a message went out."""
        settings = mail.load_settings(session, self.config)
        if settings is None or not settings.usable:
            return False
        if now_boot < self.next_attempt:
            return False
        tr = _family_translator(session)
        sent = False
        try:
            if settings.alerts:
                sent = await self._alerts(session, settings, tr) or sent
            if settings.weekly:
                sent = await self._weekly(session, settings, tr, now_wall) or sent
        except mail.MailError as exc:
            self.failures += 1
            wait = BACKOFF[min(self.failures - 1, len(BACKOFF) - 1)]
            self.next_attempt = now_boot + wait
            log.warning("mail to the parent failed (%s); trying again in %s s", exc, wait)
            session.rollback()
            return False
        self.failures = 0
        return sent

    # ------------------------------------------------------------- alerts

    async def _alerts(
        self, session: Session, settings: mail.MailSettings, tr: Translator
    ) -> bool:
        stored = repo.get_setting(session, LAST_AUDIT, "")
        newest = int(session.scalar(select(func.max(AuditLog.id))) or 0)
        if not stored:
            # Mail was just turned on: start from now, not from the family's history.
            repo.set_setting(session, LAST_AUDIT, str(newest))
            for alert in session.scalars(select(AiAlert).where(AiAlert.notified.is_(False))):
                alert.notified = True
            session.commit()
            return False
        last = int(stored)
        rows = list(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.id > last,
                    (AuditLog.severity == "alert") | AuditLog.action.in_(MAILED_ACTIONS),
                )
                .order_by(AuditLog.id)
                .limit(MAX_ROWS)
            )
        )
        assistant = list(
            session.scalars(
                select(AiAlert).where(AiAlert.notified.is_(False)).order_by(AiAlert.id)
            )
        )
        if not rows and not assistant:
            if newest > last:
                repo.set_setting(session, LAST_AUDIT, str(newest))
                session.commit()
            return False

        names = _child_names(session)
        parents = repo.parent_names(session)
        lines = [_describe(row, names, tr, parents) for row in rows]
        # Never the words: a refused search or question stays on the machine,
        # and the mail says where to read it (D-165, D-222).
        lines += [
            tr("mail.search_alert", name=names.get(alert.child_id, "?"))
            if alert.app == repo.SEARCH_ALERT_APP
            else tr(
                "mail.assistant_alert",
                name=names.get(alert.child_id, "?"),
                category=tr(f"assistant.category.{alert.category}"),
            )
            for alert in assistant
        ]
        subject = tr("mail.alerts.subject", count=len(lines))
        body = "\n".join(f"- {line}" for line in lines) + "\n\n" + tr("mail.footer")
        await asyncio.to_thread(mail.send, settings, subject, body)

        repo.set_setting(session, LAST_AUDIT, str(rows[-1].id if rows else newest))
        for alert in assistant:
            alert.notified = True
        session.commit()
        log.info("mailed %s alert line(s) to the parent", len(lines))
        return True

    # ------------------------------------------------------------- weekly

    async def _weekly(
        self,
        session: Session,
        settings: mail.MailSettings,
        tr: Translator,
        now_wall: dt.datetime,
    ) -> bool:
        if now_wall.weekday() != 6 or now_wall.hour < WEEKLY_HOUR:
            return False
        week = now_wall.date().isoformat()
        if repo.get_setting(session, LAST_WEEK, "") == week:
            return False
        body = weekly_summary(session, tr, now_wall.date(), self.config)
        await asyncio.to_thread(mail.send, settings, tr("mail.weekly.subject"), body)
        repo.set_setting(session, LAST_WEEK, week)
        session.commit()
        return True


def weekly_summary(session: Session, tr: Translator, last_day: dt.date, config: Config) -> str:
    """Each child's time on each of the last seven days, the total, and the
    week's top highlights from the insights screen (D-206)."""
    first = last_day - dt.timedelta(days=6)
    paragraphs: list[str] = [tr("mail.weekly.intro")]
    for child in session.scalars(select(Child).order_by(Child.id)):
        rows = {
            row.day: row.general_s
            for row in session.scalars(
                select(LedgerDay).where(
                    LedgerDay.child_id == child.id,
                    LedgerDay.day >= first,
                    LedgerDay.day <= last_day,
                )
            )
        }
        total = sum(rows.values())
        days = [
            f"  {tr(f'day.{day.weekday()}')}: {_minutes(rows.get(day, 0), tr)}"
            for day in (first + dt.timedelta(days=n) for n in range(7))
        ]
        lines = [tr("mail.weekly.child", name=child.display_name, total=_minutes(total, tr))]
        lines += days
        highlights = _highlights(session, child, tr, first, last_day, config)
        if highlights:
            lines.append("  " + tr("insights.mail.highlights"))
            lines += [f"  - {text}" for text in highlights]
        paragraphs.append("\n".join(lines))
    paragraphs.append(tr("mail.footer"))
    return "\n\n".join(paragraphs)


def _highlights(
    session: Session,
    child: Child,
    tr: Translator,
    first: dt.date,
    last_day: dt.date,
    config: Config,
) -> list[str]:
    """The week's top highlights for one child, as the panel words them.

    Only the sentences: a mail has no setting to link to, and none of them
    names a site or repeats what a child typed (D-177), so nothing the panel
    keeps to itself goes into a mailbox. A failure costs the highlights, never
    the mail: the minutes above are what the parent signed up for.
    """
    try:
        span = insights.Span(first, last_day)
        found = insights.insights_for(session, child, insights.Period.WEEK, last_day, span)
        chosen = insights.highlights(
            found, insights.context_for(session, child, config), MAIL_HIGHLIGHTS
        )
    except Exception:
        log.exception("could not work out the week's highlights for child %s", child.id)
        return []
    return [insights.say(tr, h.key, h.params) for h in chosen]


def _minutes(seconds: int, tr: Translator) -> str:
    return tr("mail.minutes", minutes=round(seconds / 60))


def _child_names(session: Session) -> dict[int, str]:
    return {child.id: child.display_name for child in session.scalars(select(Child))}


def _describe(
    row: AuditLog,
    names: dict[int, str],
    tr: Translator,
    parents: dict[int, str] | None = None,
) -> str:
    what = tr(f"audit.action.{row.action}")
    target = row.target or ""
    if target.startswith("child:") and target[6:].isdigit():
        return f"{names.get(int(target[6:]), '?')}: {what}"
    if target.startswith("parent:") and target[7:].isdigit():
        # "Baba: Ebeveyn eklendi" - which parent the line is about (D-218).
        name = (parents or {}).get(int(target[7:]), "")
        return f"{name}: {what}" if name else what
    return what


def _family_translator(session: Session) -> Translator:
    family = repo.get_family(session)
    return translator(normalise_locale(family.locale if family else None))
