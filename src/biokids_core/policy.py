"""The policy engine.

One pure function answers the only question that matters: *given this child, this
moment, and this activity, what should happen?* No I/O, no clock access, no
logging — `now_wall` is a parameter and the result is a value. Everything that
can be wrong about BioKids' rules can therefore be wrong in a unit test first.

Precedence is fixed by `ARCHITECTURE.md` §4 and the order is load-bearing:

    1. active penalty / timeout
    2. outside allowed time window
    3. quota exhausted
    4. forced break due
    5. category limit exhausted
    6. app not in the child's catalog
    7. URL blocked by filter
    8. AI risk above the block threshold
    9. allow

What this engine does *not* do is run the end-of-time sequence. It reports
`quota exhausted`; the daemon owns the 120-second countdown and the configured
end action, because those depend on how long ago the lock happened and that is
state, not policy.
"""

from __future__ import annotations

import datetime as dt

from biokids_core.types import (
    Action,
    ActivityContext,
    Category,
    ChildPolicy,
    Decision,
    LedgerState,
    Reason,
    SpeechRequest,
)

__all__ = ["evaluate", "general_allowance_seconds", "remaining_seconds"]

_UNLIMITED = 10**9
"""Stands in for "no constraint" when taking a minimum. Larger than any real day."""


def evaluate(
    policy: ChildPolicy | None,
    state: LedgerState,
    ctx: ActivityContext,
    now_wall: dt.datetime,
) -> Decision:
    """Decide what should happen to one child, right now.

    `policy` is None for a user BioKids does not manage — the parent's own
    account, or a child added to the system but never configured. That is not a
    failure and must never lock anyone out: principle #3 ("fail safe, not
    silent") is about our own components dying, not about an unmanaged user.
    """
    if policy is None or not policy.enabled:
        return Decision(
            allow=True,
            action=Action.NONE,
            reason_code=Reason.NO_POLICY,
            remaining_seconds=0,
        )

    remaining = remaining_seconds(policy, state, ctx, now_wall)

    # 1 — an active penalty overrides everything, including an open window.
    if state.penalty_remaining_s > 0:
        return Decision(
            allow=False,
            action=Action.TIMEOUT,
            reason_code=Reason.PENALTY_ACTIVE,
            remaining_seconds=0,
        )

    # 2 — outside the allowed window. Checked before the quota so that a child
    #     with hours of quota left still stops at bedtime.
    if not policy.window_open(now_wall):
        # Between two of today's windows, or after the last one? The first is a
        # pause and the second is the end of the day, and they were the same
        # answer: a parent with "07:30-08:15" and "16:00-19:00" had the machine
        # power itself off at 08:17 while the child was told "see you tomorrow".
        opens_in = policy.seconds_until_window_opens(now_wall)
        waiting = opens_in is not None
        return Decision(
            allow=False,
            action=Action.LOCK,
            reason_code=Reason.WINDOW_WAITING if waiting else Reason.WINDOW_CLOSED,
            speak=SpeechRequest(
                event="window_waiting" if waiting else "time_up",
                vars={
                    "name": policy.display_name,
                    "minutes": max(1, round((opens_in or 0) / 60)),
                },
                priority="high",
                expression="neutral" if waiting else "sleepy",
            ),
            remaining_seconds=0,
        )

    # 3 — quota exhausted.
    quota_left = general_allowance_seconds(policy, state) - state.general_used_s
    if quota_left <= 0:
        return Decision(
            allow=False,
            action=Action.LOCK,
            reason_code=Reason.QUOTA_EXHAUSTED,
            speak=SpeechRequest(
                event="time_up",
                vars={"name": policy.display_name},
                priority="high",
                expression="neutral",
            ),
            remaining_seconds=0,
        )

    # 4 — forced break. An already-running break comes before a newly due one, so
    #     the reason code the child's screen shows does not flap on the boundary.
    if state.break_remaining_s > 0:
        return Decision(
            allow=False,
            action=Action.LOCK,
            reason_code=Reason.BREAK_ACTIVE,
            remaining_seconds=remaining,
        )
    if policy.break_enabled and state.seconds_since_break_s >= policy.break_work_min * 60:
        return Decision(
            allow=False,
            action=Action.LOCK,
            reason_code=Reason.BREAK_DUE,
            speak=SpeechRequest(
                event="break_start",
                vars={"minutes": policy.break_rest_min},
                expression="sleepy",
            ),
            remaining_seconds=remaining,
        )

    # 5 — category limit for whatever is on screen.
    if ctx.category is not None and _category_exhausted(policy, state, ctx.category):
        return Decision(
            allow=False,
            action=Action.BLOCK,
            reason_code=Reason.CATEGORY_EXHAUSTED,
            remaining_seconds=remaining,
        )

    # 6 — an application the parent did not enable for this child.
    if not ctx.app_allowed:
        return Decision(
            allow=False,
            action=Action.BLOCK,
            reason_code=Reason.APP_NOT_ALLOWED,
            remaining_seconds=remaining,
        )

    # 7 — the filter said no.
    if ctx.url_blocked:
        return Decision(
            allow=False,
            action=Action.BLOCK,
            reason_code=Reason.FILTER_BLOCKED,
            speak=SpeechRequest(
                event="blocked",
                vars={"name": policy.display_name},
                priority="high",
                expression="concerned",
            ),
            remaining_seconds=remaining,
        )

    # 8 — the AI layer scored this above the child's block threshold. Whether that
    #     also starts a timeout is the parent's choice and defaults to off (D-80).
    if policy.ai_enabled and ctx.ai_score >= ctx.ai_block_threshold:
        return Decision(
            allow=False,
            action=Action.TIMEOUT if policy.ai_block_enabled else Action.BLOCK,
            reason_code=Reason.AI_BLOCKED,
            speak=SpeechRequest(
                event="blocked",
                vars={"name": policy.display_name},
                priority="high",
                expression="concerned",
            ),
            remaining_seconds=remaining,
        )

    # 9 — allowed. Possibly with a warning, if we have crossed 10, 5 or 1 minute.
    threshold = _warn_threshold(policy, remaining, ctx)
    if threshold is not None:
        return Decision(
            allow=True,
            action=Action.WARN,
            reason_code=Reason.QUOTA_WARNING,
            speak=SpeechRequest(
                event=f"warn_{threshold}",
                vars={"name": policy.display_name, "minutes": threshold},
                priority="high" if threshold == 1 else "normal",
                expression="neutral",
            ),
            remaining_seconds=remaining,
        )

    return Decision(
        allow=True,
        action=Action.NONE,
        reason_code=Reason.ALLOWED,
        remaining_seconds=remaining,
    )


# --------------------------------------------------------------------- helpers


def earned_lands_in_general(policy: ChildPolicy) -> bool:
    """Whether earned minutes raise the day's allowance rather than the Games cap.

    D-45 puts earned time into Oyun: learning buys game time. That needs a Games
    limit to raise. When there is no limit on Games - which is every
    installation until per-category limits ship (D-44), and any family that
    simply never set one - "raise the Games ceiling" means adding minutes to an
    allowance that was already unlimited, so the child earns nothing at all
    while being congratulated for it.

    So: the parent's setting decides when there is a Games limit to honour, and
    when there is not, the only place the minutes can go is the general
    allowance. This is not a departure from D-45; it is what D-45 means in a
    configuration it did not consider.
    """
    if not policy.earn_enabled:
        return False
    return policy.earn_spendable_general or Category.GAMES not in policy.category_limits_min


def general_allowance_seconds(policy: ChildPolicy, state: LedgerState) -> int:
    """Today's total screen-time allowance in seconds.

    The quota for this kind of day, plus whatever the parent granted by hand,
    plus earned minutes when they land here rather than in Games (D-45).
    """
    base = policy.quota_minutes(state.day) * 60 + state.granted_s
    if earned_lands_in_general(policy):
        base += min(state.earned_s, policy.earn_cap_min * 60)
    return base


def remaining_seconds(
    policy: ChildPolicy,
    state: LedgerState,
    ctx: ActivityContext,
    now_wall: dt.datetime,
) -> int:
    """The child's effective remaining time: the tightest of every constraint.

    remaining = (quota + earned) - used
                ∩ (seconds left in the current window)
                ∩ (seconds left in the active category)
                - (penalty active => 0)
    """
    if state.penalty_remaining_s > 0:
        return 0

    quota_left = max(0, general_allowance_seconds(policy, state) - state.general_used_s)

    window_left = policy.seconds_until_window_closes(now_wall)
    if window_left is None:
        window_left = _UNLIMITED

    category_left = _UNLIMITED
    if ctx.category is not None:
        category_left = _category_remaining(policy, state, ctx.category)

    return max(0, min(quota_left, window_left, category_left))


def _category_allowance_seconds(policy: ChildPolicy, state: LedgerState, cat: Category) -> int:
    limit = policy.category_limits_min.get(cat)
    if limit is None:
        return _UNLIMITED
    allowance = limit * 60
    # Earned minutes land in Games unless they went to the general allowance
    # instead (D-45). Exactly one of the two, never both and never neither.
    if cat is Category.GAMES and policy.earn_enabled and not earned_lands_in_general(policy):
        allowance += min(state.earned_s, policy.earn_cap_min * 60)
    return allowance


def _category_remaining(policy: ChildPolicy, state: LedgerState, cat: Category) -> int:
    allowance = _category_allowance_seconds(policy, state, cat)
    if allowance >= _UNLIMITED:
        return _UNLIMITED
    return max(0, allowance - state.category_used(cat))


def _category_exhausted(policy: ChildPolicy, state: LedgerState, cat: Category) -> bool:
    return _category_remaining(policy, state, cat) <= 0


def _warn_threshold(policy: ChildPolicy, remaining: int, ctx: ActivityContext) -> int | None:
    """Which warning, if any, applies at `remaining` seconds left.

    With thresholds (10, 5, 1) and four minutes left, both 10 and 5 have been
    crossed; the child should hear "five minutes", so we take the smallest
    threshold that is still ahead of the clock. The daemon is responsible for
    saying each one only once — the engine has no memory by design.
    """
    if not ctx.session_active or remaining <= 0:
        return None
    crossed = [m for m in policy.warn_minutes if 0 < remaining <= m * 60]
    return min(crossed) if crossed else None
