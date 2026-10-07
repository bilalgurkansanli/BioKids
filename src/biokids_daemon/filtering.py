"""What the children's browser and DNS may reach (D-156, D-164, D-167, D-172).

Two things follow every change to the children's web tiles and allowed sites,
so `Supervisor.reload` rewrites both: the machine-wide Chromium managed policy
and the children's DNS filter. `check_filter` checks the filter from the outside
once a minute.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from sqlalchemy import select

from biokids_core import repo
from biokids_core.catalog import load_catalog
from biokids_core.db.models import FilterRule
from biokids_core.errors import ConfigError
from biokids_core.types import FilterMode
from biokids_daemon.base import SupervisorBase, log
from biokids_daemon.dnsfilter import DnsFilter

FILTER_CHECK_SECONDS = 60
"""How often the children's DNS filter is checked from the outside (D-172)."""
FILTER_SETTLE_SECONDS = 30
"""The first check waits this long after the daemon starts."""
FILTER_SETTLE_TRIES = 5
FILTER_SETTLE_PAUSE = 2.0


def web_tile_pattern(url: str) -> str:
    """The host of a web tile the catalog no longer describes, without `www.`."""
    return (urlsplit(url).hostname or "").removeprefix("www.")


class FilteringMixin(SupervisorBase):
    """The kiosk policy, the DNS filter, and the once-a-minute filter check."""

    @property
    def dns_filter(self) -> DnsFilter:
        """The children's DNS filter (D-167), its files under the data directory."""
        if self._dns_filter is None:
            self._dns_filter = DnsFilter(
                self.config.paths.data_dir / "dns", panel_port=self.config.panel.port
            )
        return self._dns_filter

    def _write_kiosk_policy(self) -> None:
        """Put the child's Chromium under managed policy (T5.2).

        Written here because `/etc/chromium/policies/managed` is root-owned and
        this unit's `ReadWritePaths` names it; the launcher runs as the child.
        One file covers every child, because Chromium reads only the
        machine-wide directory: the launcher's origin and the hosts of every web
        tile enabled for any child (D-156).
        """
        from biokids_core.browser_policy import POLICY_FILENAME, kiosk_policy

        directory = self.config.paths.chromium_policy_dir
        origin = f"{self.config.shell.host}:{self.config.shell.port}"
        path = directory / POLICY_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(
                    kiosk_policy(origin, self._web_tile_hosts(), self._browser_mode()),
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError as exc:
            # Loud, and not fatal: a daemon that refuses to start without
            # Chromium would be worse than a kiosk without a policy.
            log.error(
                "could not write the Chromium managed policy to %s (%s). If a child "
                "uses the browser it will have NO policy: no URL allowlist, DevTools "
                "available, downloads unrestricted.",
                path,
                exc,
            )
            return
        log.info("Chromium managed policy written to %s for %s", path, origin)

    def _browser_mode(self) -> str:
        """`allowlist` if any managed child is on "selected sites only" (D-167).

        Chromium reads one policy for the whole machine, so the strictest child
        decides.
        """
        for runtime in self.children.values():
            policy = runtime.policy
            if policy is not None and policy.filter_mode == FilterMode.ALLOWLIST:
                return "allowlist"
        return "blocklist"

    def _apply_dns_filter(self) -> None:
        """Every managed child's DNS through the filter (D-167)."""
        users = [r.unix_user for r in self.children.values() if r.policy is not None]
        hosts = {pattern.split("/", 1)[0] for pattern in self._web_tile_hosts()}
        hosts.discard(f"{self.config.shell.host}:{self.config.shell.port}")
        self.dns_filter.apply(users, sorted(hosts))

    def _web_tile_hosts(self) -> list[str]:
        """What the web tiles enabled for any child may open, `www.` dropped.

        A host without a scheme also allows its subdomains in Chromium's URL
        filter, so the bare domain covers `www.` and not the other way round. A
        tile's catalog entry may name exactly what it needs instead (D-164). The
        filter governs pages and frames, not what a page loads, so a site's CDN
        needs no entry.
        """
        try:
            catalog = {entry.slug: entry for entry in load_catalog(config=self.config)}
        except ConfigError:
            # No catalog file: every tile gets its own host, which is safe.
            catalog = {}
        patterns: set[str] = set()
        for child_id in self.children:
            for _link, app in repo.list_child_apps(self.session, child_id):
                if app.kind != "web" or not app.url:
                    continue
                entry = catalog.get(app.slug)
                if entry is not None:
                    patterns.update(entry.allowed_patterns())
                else:
                    pattern = web_tile_pattern(app.url)
                    if pattern:
                        patterns.add(pattern)
        # Sites a parent said yes to when a child asked (D-173).
        patterns.update(
            rule.pattern
            for rule in self.session.scalars(
                select(FilterRule).where(
                    FilterRule.kind == "allow", FilterRule.child_id.in_(list(self.children))
                )
            )
            if rule.pattern
        )
        return sorted(patterns)

    async def check_filter(self, now_boot: float) -> None:
        """Check the children's DNS filter from the outside, once a minute (D-172).

        Is the redirect there, and does the filter refuse a name every adult
        list has? Open - the children could reach what the lists close - is
        principle 3: one repair is tried, and if the filter is still open every
        child at the machine is locked, and unlocked again when it closes. Not
        answering is already closed (no DNS, no internet) and is reported, not
        locked.
        """
        if self._filter_checked_at is None:
            # Not in the first half-minute: dnsmasq is restarted alongside the
            # daemon and takes a few seconds to load its lists.
            self._filter_checked_at = now_boot - FILTER_CHECK_SECONDS + FILTER_SETTLE_SECONDS
            return
        if now_boot - self._filter_checked_at < FILTER_CHECK_SECONDS:
            return
        self._filter_checked_at = now_boot
        users = [r.unix_user for r in self.children.values() if r.policy is not None]
        if not users:
            self.filter_health = None
            return
        health = await asyncio.to_thread(self.dns_filter.verify, users)
        if health.open or not health.answering:
            log.warning("the DNS filter is not right (%s); applying it again", health)
            await asyncio.to_thread(self._apply_dns_filter)
            # The repair restarts dnsmasq; give it the seconds it needs to load.
            for _ in range(FILTER_SETTLE_TRIES):
                health = await asyncio.to_thread(self.dns_filter.verify, users)
                if health.answering and not health.open:
                    break
                await asyncio.sleep(FILTER_SETTLE_PAUSE)

        previous = self.filter_health
        self.filter_health = health
        if health.open:
            if previous is None or not previous.open:
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="filter_failed",
                    detail={"redirect": health.redirect, "filtering": health.filtering},
                    severity="alert",
                )
            for runtime in self.children.values():
                presence = self.watcher.presence(runtime.unix_user)
                if presence.lockable_session_ids:
                    runtime.locked_for_filter = True
                    await self._lock(runtime, presence, only_unlocked=False)
        elif not health.answering:
            if previous is None or previous.answering or previous.open:
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="filter_down",
                    detail={"error": self.dns_filter.last_error},
                    severity="alert",
                )
        elif previous is not None and (previous.open or not previous.answering):
            repo.audit(self.session, actor="daemon", action="filter_restored")
        if not health.open:
            for runtime in self.children.values():
                if runtime.locked_for_filter:
                    runtime.locked_for_filter = False
                    await self._unlock(runtime)
        self.session.commit()
