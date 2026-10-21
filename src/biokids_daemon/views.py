"""What the panel's Today screen and the child's launcher draw, each in one call."""

from __future__ import annotations

from typing import Any

from biokids_core import album, repo
from biokids_core.policy import (
    bedtime_in,
    evaluate,
    general_allowance_seconds,
    no_limit_ring_seconds,
)
from biokids_core.types import ActivityContext, Category
from biokids_daemon.access_requests import AccessRequestsMixin
from biokids_daemon.filtering import FilteringMixin


class ViewsMixin(AccessRequestsMixin, FilteringMixin):
    """Read-only snapshots: `status()` for the panel, `shell_state()` for the launcher."""

    def status(self) -> dict[str, Any]:
        """Everything the Today screen needs, for every child, in one call."""
        reading = self.clock.read()
        day = reading.wall.date()
        children: list[dict[str, Any]] = []

        for runtime in self.children.values():
            presence = self.watcher.presence(runtime.unix_user)
            state = self.ledger.state(runtime.child_id, day)
            decision = evaluate(
                runtime.policy,
                state,
                ActivityContext(
                    app_slug=runtime.app_slug or None,
                    category=runtime.category,
                    session_active=presence.active,
                ),
                reading.wall,
            )
            policy = runtime.policy
            children.append(
                {
                    "child_id": runtime.child_id,
                    "unix_user": runtime.unix_user,
                    "display_name": runtime.display_name,
                    "avatar": runtime.avatar,
                    "locale": runtime.locale,
                    "age_band": runtime.age_band,
                    "managed": policy is not None,
                    "online": presence.active,
                    "present": presence.present,
                    "idle": presence.idle,
                    "app": runtime.app_name or runtime.app_slug,
                    "app_slug": runtime.app_slug,
                    "category": runtime.category.value if runtime.category else "",
                    "remaining_seconds": decision.remaining_seconds,
                    "remaining_minutes": decision.remaining_minutes,
                    "used_seconds": state.general_used_s,
                    "quota_seconds": (policy.quota_minutes(day) * 60) if policy else 0,
                    "earned_seconds": state.earned_s,
                    "granted_seconds": state.granted_s,
                    "penalty_seconds": state.penalty_remaining_s,
                    "break_seconds": state.break_remaining_s,
                    "allow": decision.allow,
                    "action": decision.action.value,
                    "reason_code": decision.reason_code,
                    "end_action": policy.end_action.value if policy else "",
                    # A day with no daily limit (D-215) draws its ring against
                    # the window it is in; None on every other day.
                    "ring_seconds": no_limit_ring_seconds(policy, state, reading.wall),
                }
            )

        return {
            "ticks": self.ticks,
            "day": day.isoformat(),
            "children": children,
            # D-167: so a parent can see that harmful sites are really being
            # stopped, and why not when they are not.
            "dns_filter": {
                # From the last outside check, not a `systemctl` call per poll.
                "running": self.filter_health is None
                or (self.filter_health.answering and not self.filter_health.open),
                "open": bool(self.filter_health and self.filter_health.open),
                "error": self.dns_filter.last_error,
                "mode": self._browser_mode(),
            },
        }

    def shell_state(self, child_id: int) -> dict[str, Any]:
        """Everything the child's launcher draws, in one call.

        The launcher runs as the child and cannot read the database - it is 0600
        root - so identity, tiles and remaining time come from here. It renders
        what it is given and has no way to invent a number.
        """
        runtime = self._require(child_id)
        reading = self.clock.read()
        day = reading.wall.date()
        presence = self.watcher.presence(runtime.unix_user)
        state = self.ledger.state(child_id, day)
        policy = runtime.policy

        decision = evaluate(
            policy,
            state,
            ActivityContext(
                app_slug=runtime.app_slug or None,
                category=runtime.category,
                session_active=True,
            ),
            reading.wall,
        )

        child = repo.get_child(self.session, child_id)
        tiles: list[dict[str, Any]] = []
        if child is not None:
            for link, app in repo.list_child_apps(self.session, child_id):
                if app.kind == "__gone__":
                    continue
                tiles.append(
                    {
                        "slug": app.slug,
                        "name": app.name(runtime.locale),
                        "description": app.description(runtime.locale),
                        "icon": app.icon,
                        "kind": app.kind,
                        "url": app.url,
                        "exec": app.exec_cmd,
                        "category": repo.child_app_category(link, app).value,
                    }
                )

        # The whole allowance, earned minutes included: the launcher draws its
        # ring as remaining/quota, which against the bare quota would stay full.
        quota = general_allowance_seconds(policy, state) if policy else 0
        # The day ending for the night, within ten minutes or already (D-214):
        # the warnings and the end screen are drawn for bedtime.
        night = bedtime_in(policy, state, reading.wall)
        ring = no_limit_ring_seconds(policy, state, reading.wall)
        return {
            "child_id": child_id,
            "display_name": runtime.display_name,
            "avatar": runtime.avatar,
            "locale": runtime.locale,
            "age_band": runtime.age_band,
            "managed": policy is not None,
            "supervision_indicator": child.supervision_indicator if child else "badge",
            "first_run_done": bool(child.first_run_done) if child else True,
            "tiles": tiles,
            "asked_for_time": self.pending_request(child_id, "time"),
            "remaining_seconds": decision.remaining_seconds,
            "remaining_minutes": decision.remaining_minutes,
            "quota_seconds": ring if ring is not None else quota + state.granted_s,
            "used_seconds": state.general_used_s,
            "earned_seconds": state.earned_s,
            "break_seconds": state.break_remaining_s,
            "penalty_seconds": state.penalty_remaining_s,
            "allow": decision.allow,
            "action": decision.action.value,
            "reason_code": decision.reason_code,
            "idle": presence.idle,
            # Which end action is configured (D-43), so the time-up screen does
            # not tell every child the computer is about to shut down.
            "end_action": policy.end_action.value if policy else "",
            # Whether to show the assistant's tile and its microphone button
            # (D-165, D-166): switched on for this child *and* able to answer -
            # installed, with a model downloaded (D-198). The daemon re-checks
            # both on every question.
            "assistant": {
                "enabled": bool(
                    policy and policy.assistant_enabled and self.assistant_ready.get("chat")
                ),
                "voice": bool(
                    policy
                    and policy.assistant_enabled
                    and policy.assistant_voice
                    and self.assistant_ready.get("voice")
                ),
            },
            "countdown_seconds": self._countdown_remaining(
                runtime, day, reading.boot, reading.boot_id
            ),
            "bedtime": night is not None,
            "bedtime_seconds": night,
            # The parent's own search words (D-202), which the launcher adds to
            # the default list it serves the browser extension.
            "search_words": repo.search_words(self.session),
            # The sticker album (D-211): the days that earned one, and today's
            # learning so far, from the ledger, which knows the seconds it has
            # not written yet. Never minutes: nothing here changes the time.
            "album": album.for_shell(
                self.session, child_id, day, state.category_used_s.get(Category.LEARNING, 0)
            ),
        }
