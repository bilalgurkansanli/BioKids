"""Access requests, "Anneme sor" (D-65, D-173): a child asks for time, a site or an app.

The parent answers from the panel, and the child hears the answer either way.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select

from biokids_core import repo
from biokids_core.catalog import enable_for_child
from biokids_core.db.models import AccessRequest, App, FilterRule, utcnow
from biokids_core.ipc import EventKind
from biokids_core.types import SpeechRequest
from biokids_daemon.base import log
from biokids_daemon.filtering import web_tile_pattern
from biokids_daemon.operations import OperationsMixin

REQUEST_KINDS = ("time", "site", "app", "search")
"""`search` is a search the browser refused (D-202). It carries nothing - the
words never leave the browser - and there is nothing to allow: the parent is
asked to talk with the child, and answers that they will."""
REQUESTS_PER_HOUR = 6
"""A child may ask this often. Asking again for the same thing while it is still
waiting is the same request, not another one."""
REQUEST_EXPIRY = dt.timedelta(hours=12)
"""A request nobody answered is gone by the next morning: "more time" asked for
yesterday is not a question about today."""


class AccessRequestsMixin(OperationsMixin):
    """Filing a child's question, and carrying out the parent's answer."""

    def file_access_request(self, child_id: int, url: str, kind: str = "site") -> int:
        """ "Anneme sor" (D-65, D-173). Turns the filter from a wall into a conversation.

        Returns the request's id, or 0 when the child has asked too often. The
        same question asked twice while it waits is one request: a child who
        presses the button five times is asking once, impatiently.
        """
        runtime = self._require(child_id)
        kind = kind if kind in REQUEST_KINDS else "site"
        url = url[:1024] if kind not in {"time", "search"} else ""
        self._expire_requests()
        waiting = self.session.scalar(
            select(AccessRequest).where(
                AccessRequest.child_id == child_id,
                AccessRequest.status == "pending",
                AccessRequest.kind == kind,
                AccessRequest.url == url,
            )
        )
        if waiting is not None:
            return int(waiting.id)
        recent = self.session.scalar(
            select(func.count())
            .select_from(AccessRequest)
            .where(
                AccessRequest.child_id == child_id,
                AccessRequest.created_at >= utcnow() - dt.timedelta(hours=1),
            )
        )
        if int(recent or 0) >= REQUESTS_PER_HOUR:
            log.info(
                "%s asked %s times this hour; not filing another", runtime.unix_user, recent
            )
            return 0
        row = AccessRequest(child_id=child_id, url=url, kind=kind, status="pending")
        self.session.add(row)
        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="access_requested",
            target=f"child:{child_id}",
            detail={"url": url, "kind": kind},
        )
        self.session.commit()
        self.bus.publish(
            EventKind.ACCESS_REQUEST,
            child_id=child_id,
            url=url,
            request_kind=kind,
            request_id=row.id,
        )
        return int(row.id)

    def _expire_requests(self) -> None:
        cutoff = utcnow() - REQUEST_EXPIRY
        for row in self.session.scalars(
            select(AccessRequest).where(
                AccessRequest.status == "pending", AccessRequest.created_at < cutoff
            )
        ):
            row.status = "expired"
            row.decided_at = utcnow()

    def pending_request(self, child_id: int, kind: str) -> bool:
        return (
            self.session.scalar(
                select(AccessRequest.id).where(
                    AccessRequest.child_id == child_id,
                    AccessRequest.status == "pending",
                    AccessRequest.kind == kind,
                    AccessRequest.created_at >= utcnow() - REQUEST_EXPIRY,
                )
            )
            is not None
        )

    async def decide_request(
        self, request_id: int, approve: bool, minutes: int = 0, actor: str = "parent"
    ) -> dict[str, Any]:
        """A parent's answer to a child's question, carried out and told to the child.

        Yes to more time is a grant (which also lets a locked child back in);
        yes to a site allows its host for that child, by policy and by DNS; yes
        to an app enables it on their screen. No is no, said kindly. Either way
        the child hears it from Zio or Sio and sees it on screen, because a
        question that disappears into silence teaches a child not to ask.
        """
        row = self.session.get(AccessRequest, request_id)
        if row is None or row.status != "pending":
            return {"ok": False, "error": "error.not_found"}
        runtime = self._require(row.child_id)
        if row.kind == "search":
            # Nothing to allow (D-202): the answer is "not now, we'll talk".
            approve = False
        if approve and row.kind == "time" and not 0 < minutes <= 12 * 60:
            return {"ok": False, "error": "error.invalid_input"}

        row.status = "approved" if approve else "denied"
        row.decided_at = utcnow()
        row.minutes = minutes if approve and row.kind == "time" else 0
        host = web_tile_pattern(row.url) if row.url else ""
        if approve and row.kind == "site" and host:
            self.session.add(
                FilterRule(child_id=row.child_id, kind="allow", pattern=host, source="request")
            )
        if approve and row.kind == "app" and self.session.get(App, row.url) is not None:
            enable_for_child(self.session, row.child_id, [row.url], replace=False)
        repo.audit(
            self.session,
            actor=actor,
            action="request_approved" if approve else "request_denied",
            target=f"child:{row.child_id}",
            detail={"kind": row.kind, "url": row.url, "minutes": row.minutes},
        )
        self.session.commit()

        if approve and row.kind == "time":
            await self.grant_time(row.child_id, minutes * 60, actor)
        elif approve:
            self.reload()

        self.bus.publish(
            EventKind.REQUEST_DECIDED,
            child_id=row.child_id,
            approved=approve,
            request_kind=row.kind,
            minutes=row.minutes,
        )
        self._speak(
            runtime,
            SpeechRequest(
                event="request_approved" if approve else "request_denied",
                vars={"minutes": row.minutes, "name": runtime.display_name},
                priority="high",
                expression="happy" if approve else "neutral",
            ),
        )
        return {"ok": True, "status": row.status}
