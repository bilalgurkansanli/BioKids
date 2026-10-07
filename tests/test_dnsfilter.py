"""D-167: the children's DNS filter.

What it must never do matters as much as what it must: never filter anyone but
the children, never block a site a parent enabled, never leave a half-written
file for dnsmasq to load, never be gone round by DNS-over-TLS.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from biokids_daemon import dnsfilter
from biokids_daemon.dnsfilter import (
    DNS_PORT,
    DnsFilter,
    FilterFiles,
    load_domains,
    render_blocklist,
    render_conf,
    render_nft,
)


def test_the_shipped_lists_are_there_and_large() -> None:
    domains = load_domains()
    assert len(domains) > 200_000, "the adult list alone is over 200,000 domains"
    assert "dns.google" in domains or any(d.endswith("dns.google") for d in domains)


def test_a_blocked_domain_answers_nxdomain_with_its_subdomains() -> None:
    assert (
        render_blocklist(["b.example", "a.example"]) == "local=/a.example/\nlocal=/b.example/\n"
    )


def test_only_the_children_are_redirected() -> None:
    rules = render_nft([1001, 1002, 1001])
    assert "elements = { 1001, 1002 }" in rules
    assert f"meta skuid @children udp dport 53 redirect to :{DNS_PORT}" in rules
    assert f"meta skuid @children tcp dport 53 redirect to :{DNS_PORT}" in rules
    filtering = rules.split("chain launchers", 1)[0]
    for line in filtering.splitlines():
        if "redirect to" in line or line.rstrip().endswith("reject"):
            assert "@children" in line, f"a rule that is not about children: {line}"


def test_with_no_children_nothing_is_redirected() -> None:
    rules = render_nft([])
    assert "elements" not in rules
    assert "type uid" in rules


def test_the_filter_cannot_simply_be_gone_round() -> None:
    rules = render_nft([1001])
    assert "tcp dport 853 reject" in rules, "DNS over TLS"
    assert "1.1.1.1" in rules and "8.8.8.8" in rules, "DoH by address"


def test_the_panel_is_closed_to_children_every_way_in() -> None:
    """By port, whatever name or address was typed, and the whole tailnet."""
    rules = render_nft([1001], panel_port=7878)
    assert "meta skuid @children tcp dport 7878 reject" in rules
    assert "meta skuid @children ip daddr 100.64.0.0/10 reject" in rules
    assert "meta skuid @children ip6 daddr fd7a:115c:a1e0::/48 reject" in rules


def test_a_public_name_cannot_answer_with_a_private_address(tmp_path: Path) -> None:
    """DNS rebinding: 127.0.0.1.nip.io would otherwise be a way to the launcher."""
    assert "stop-dns-rebind" in render_conf(FilterFiles(tmp_path)).splitlines()


def test_the_rules_replace_themselves_in_one_transaction() -> None:
    """`destroy` then the table, in one file: nft -f applies it atomically."""
    rules = render_nft([1001])
    assert rules.index("destroy table inet biokids") < rules.index("table inet biokids {")


def test_a_site_a_parent_enabled_is_never_blocked(tmp_path: Path) -> None:
    conf = render_conf(FilterFiles(tmp_path), allowed_hosts=["youtubekids.com", "eba.gov.tr"])
    assert "server=/youtubekids.com/#" in conf
    assert "server=/eba.gov.tr/#" in conf


def test_safe_search_is_part_of_the_answer(tmp_path: Path) -> None:
    conf = render_conf(FilterFiles(tmp_path))
    assert "cname=www.google.com,google.com,www.google.com.tr,google.com.tr," in conf
    assert "host-record=forcesafesearch.google.com," in conf
    assert "restrict.youtube.com" in conf
    assert "local=/use-application-dns.net/" in conf, "Firefox's DoH canary"


def test_it_listens_on_loopback_only_and_on_its_own_port(tmp_path: Path) -> None:
    """Never a resolver for the network: the old Depends on dnsmasq was one."""
    conf = render_conf(FilterFiles(tmp_path))
    assert f"port={DNS_PORT}" in conf
    assert "listen-address=127.0.0.1" in conf
    assert "bind-interfaces" in conf
    assert "0.0.0.0" not in conf


def test_applying_writes_everything_and_restarts_only_when_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lists = tmp_path / "lists"
    lists.mkdir()
    for category in dnsfilter.CATEGORIES:
        (lists / f"{category}.txt.gz").write_bytes(gzip.compress(b"bad.example\n"))
    calls: list[list[str]] = []

    def run(argv: list[str]) -> bool:
        calls.append(argv)
        return True

    monkeypatch.setattr(dnsfilter, "_run", run)
    monkeypatch.setattr(dnsfilter, "resolve_safe_search", lambda: {})
    monkeypatch.setattr(dnsfilter, "uids_of", lambda users: [1001] if users else [])

    filter_ = DnsFilter(tmp_path / "dns", lists_dir=lists)
    assert filter_.apply(["elif"], ["eba.gov.tr"]) is True
    assert (tmp_path / "dns" / "blocklist.conf").read_text() == "local=/bad.example/\n"
    assert ["nft", "-f", str(tmp_path / "dns" / "biokids.nft")] in calls
    assert any(c[:2] == ["systemctl", "restart"] for c in calls)

    calls.clear()
    filter_.apply(["elif"], ["eba.gov.tr"])
    assert not any(c[:2] == ["systemctl", "restart"] for c in calls), "nothing changed"
    assert ["nft", "-f", str(tmp_path / "dns" / "biokids.nft")] in calls, (
        "reloaded every time: something may have flushed it"
    )


def test_without_its_lists_the_filter_stays_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lists: the redirect still loads, dnsmasq does not start. The children
    have no internet rather than an unfiltered one (principle 3)."""
    calls: list[list[str]] = []

    def run(argv: list[str]) -> bool:
        calls.append(argv)
        return True

    monkeypatch.setattr(dnsfilter, "_run", run)
    monkeypatch.setattr(dnsfilter, "resolve_safe_search", lambda: {})
    monkeypatch.setattr(dnsfilter, "uids_of", lambda _users: [1001])
    filter_ = DnsFilter(tmp_path / "dns", lists_dir=tmp_path / "missing")
    assert filter_.apply(["elif"], []) is False
    assert filter_.last_error.startswith("blocklist")
    assert ["nft", "-f", str(tmp_path / "dns" / "biokids.nft")] in calls
    assert ["systemctl", "stop", "--no-block", dnsfilter.SERVICE] in calls
    assert not any(c[:2] == ["systemctl", "restart"] for c in calls)
    assert not (tmp_path / "dns" / "dnsmasq.conf").exists()


def test_the_redirect_is_back_before_the_login_screen() -> None:
    """nftables rules do not survive a reboot. On the first VM the daemon
    loaded them sixteen seconds after LightDM had started, and a child's DNS
    was unfiltered until then. The filter's unit loads the last rendered rules
    itself, before the display manager."""
    import configparser

    unit = (
        Path(__file__).resolve().parent.parent / "debian" / "biokids-daemon.biokids-dns.service"
    )
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(unit.read_text(encoding="utf-8"))
    assert "display-manager.service" in parser["Unit"]["Before"]
    files = FilterFiles(Path("/var/lib/biokids/dns"))
    assert parser["Service"]["ExecStartPre"] == f"+/usr/sbin/nft -f {files.nft.as_posix()}"
    assert parser["Unit"]["ConditionPathExists"] == files.conf.as_posix()


def test_each_launcher_answers_only_its_own_child() -> None:
    """D-188: a launcher acts as its child; a sibling's browser must not reach it."""
    rules = render_nft([1001, 1002], launcher_base=7879)
    assert "ip daddr 127.0.0.1 tcp dport 7880 meta skuid != 1001 reject" in rules
    assert "ip6 daddr ::1 tcp dport 7881 meta skuid != 1002 reject" in rules


def test_two_children_on_one_port_do_not_shut_each_other_out() -> None:
    """uids 100 apart share a launcher port (D-188): one rule, both allowed."""
    rules = render_nft([1001, 1101])
    assert "tcp dport 7880 meta skuid != { 1001, 1101 } reject" in rules
    assert "meta skuid != 1001 reject" not in rules
