"""The children's DNS filter: harmful sites blocked, whatever the internet mode (D-167).

The owner asked for the open internet by default with the worst of it always
closed, and for "selected sites only" as a parent's choice. A browser policy
cannot hold three hundred thousand domains, and a filter in the browser covers
only the browser, so the blocking is done where every program asks: DNS.

    child's program ──udp/53──► nftables (child uids only) ──► dnsmasq :5354
                                                                ├ adult, gambling, malware,
                                                                │ DoH providers: NXDOMAIN
                                                                ├ Google, YouTube, Bing, DDG:
                                                                │ SafeSearch addresses
                                                                └ everything else: upstream

Only the children's accounts are redirected, by the socket owner's uid. A
parent's session, the system's updates and dnsmasq's own upstream queries are
untouched, so a broken list can never take the family's internet down
(principle 4). For a child, a filter that is not running means no DNS at all:
closed, not open (principle 3). DNS over TLS, and the well-known DNS-over-HTTPS
addresses, are refused for children so the filter cannot simply be gone round;
Chromium's own DoH is off by policy.

The lists are a public-domain snapshot shipped in the package
(`biokids_core/data/blocklists`), so the filter works on a machine that has
never been online. A site a parent enabled as a tile is never blocked.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import logging
import socket
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

DNS_PORT: Final = 5354
CATEGORIES: Final = ("adult", "gambling", "malware", "doh")
LISTS_DIR: Final = (
    Path(__file__).resolve().parent.parent / "biokids_core" / "data" / "blocklists"
)
SERVICE: Final = "biokids-dns.service"
NFT_TABLE: Final = "biokids"

#: SafeSearch by DNS. The vendors' documented hostnames, and the addresses they
#: answered with when this was written; `resolve_safe_search` asks for current
#: ones at each apply and falls back to these offline.
SAFE_SEARCH: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "forcesafesearch.google.com": (
        ("www.google.com", "google.com", "www.google.com.tr", "google.com.tr"),
        ("216.239.38.120", "2001:4860:4802:32::78"),
    ),
    "restrict.youtube.com": (
        (
            "www.youtube.com",
            "m.youtube.com",
            "youtubei.googleapis.com",
            "youtube.googleapis.com",
            "www.youtube-nocookie.com",
        ),
        ("216.239.38.120", "2001:4860:4802:32::78"),
    ),
    "strict.bing.com": (("www.bing.com",), ("150.171.27.16",)),
    "safe.duckduckgo.com": (("duckduckgo.com", "www.duckduckgo.com"), ("40.114.177.246",)),
}

#: Public DNS-over-HTTPS resolvers reached by address rather than by name.
DOH_ADDRESSES: Final = (
    "1.1.1.1",
    "1.0.0.1",
    "1.1.1.2",
    "1.0.0.2",
    "1.1.1.3",
    "1.0.0.3",
    "8.8.8.8",
    "8.8.4.4",
    "9.9.9.9",
    "149.112.112.112",
    "9.9.9.10",
    "149.112.112.10",
    "9.9.9.11",
    "149.112.112.11",
    "94.140.14.14",
    "94.140.15.15",
    "94.140.14.15",
    "94.140.15.16",
    "208.67.222.222",
    "208.67.220.220",
    "208.67.222.123",
    "208.67.220.123",
    "185.228.168.168",
    "185.228.168.9",
    "185.228.169.9",
    "76.76.2.0",
    "76.76.10.0",
    "45.90.28.0",
    "45.90.30.0",
    "194.242.2.2",
    "194.242.2.3",
    "194.242.2.4",
    "194.242.2.5",
    "194.242.2.6",
    "194.242.2.9",
)
DOH_ADDRESSES6: Final = (
    "2606:4700:4700::1111",
    "2606:4700:4700::1001",
    "2606:4700:4700::1112",
    "2606:4700:4700::1002",
    "2606:4700:4700::1113",
    "2606:4700:4700::1003",
    "2001:4860:4860::8888",
    "2001:4860:4860::8844",
    "2620:fe::fe",
    "2620:fe::9",
    "2620:fe::10",
    "2620:fe::fe:10",
    "2620:fe::11",
    "2a10:50c0::ad1:ff",
    "2a10:50c0::ad2:ff",
    "2a07:a8c0::",
    "2a07:a8c1::",
    "2a07:e340::2",
)

PANEL_PORT: Final = 7878
TAILNET4: Final = "100.64.0.0/10"
TAILNET6: Final = "fd7a:115c:a1e0::/48"
"""Tailscale's address ranges. The panel is published there (D-33); a child has
nothing to reach on the family's tailnet, and closing it by address closes the
panel under every name it has there."""


@dataclass(frozen=True, slots=True)
class FilterFiles:
    directory: Path

    @property
    def conf(self) -> Path:
        return self.directory / "dnsmasq.conf"

    @property
    def blocklist(self) -> Path:
        return self.directory / "blocklist.conf"

    @property
    def nft(self) -> Path:
        return self.directory / "biokids.nft"


def load_domains(
    lists_dir: Path = LISTS_DIR, categories: Iterable[str] = CATEGORIES
) -> set[str]:
    """Every listed domain. A list that cannot be read raises: a filter running
    without its adult list would look on while being nothing (principle 3)."""
    domains: set[str] = set()
    for category in categories:
        path = lists_dir / f"{category}.txt.gz"
        text = gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
        domains.update(line.strip() for line in text.splitlines() if line.strip())
    return domains


def render_blocklist(domains: Iterable[str]) -> str:
    """`local=/d/`: NXDOMAIN for the domain and everything under it."""
    return "".join(f"local=/{domain}/\n" for domain in sorted(domains))


def resolve_safe_search() -> dict[str, tuple[str, ...]]:
    """Each SafeSearch target's current addresses, or the built-in ones."""
    answer: dict[str, tuple[str, ...]] = {}
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(3)
    try:
        for target, (_names, fallback) in SAFE_SEARCH.items():
            try:
                found = {str(info[4][0]) for info in socket.getaddrinfo(target, 443)}
            except OSError:
                found = set()
            answer[target] = tuple(sorted(found)) or fallback
    finally:
        socket.setdefaulttimeout(previous)
    return answer


def render_conf(
    files: FilterFiles,
    *,
    allowed_hosts: Iterable[str] = (),
    safe_search: dict[str, tuple[str, ...]] | None = None,
    user: str = "dnsmasq",
) -> str:
    safe_search = safe_search or {t: f for t, (_n, f) in SAFE_SEARCH.items()}
    lines = [
        "# BioKids DNS filter for children's accounts (D-167).",
        "# Written by biokids-daemon at every start and reload; edits are lost.",
        f"port={DNS_PORT}",
        "listen-address=127.0.0.1",
        "listen-address=::1",
        "bind-interfaces",
        "no-hosts",
        "domain-needed",
        "bogus-priv",
        # No public name may answer with a private address, 127.0.0.1 included:
        # that is how a web page reaches the launcher or the router as its own
        # origin (DNS rebinding). A child has no LAN names to look up.
        "stop-dns-rebind",
        "cache-size=10000",
        "resolv-file=/etc/resolv.conf",
        f"user={user}",
        f"conf-file={files.blocklist}",
        "",
        "# Firefox's canary: answering it says 'DNS-over-HTTPS is not allowed here'.",
        "local=/use-application-dns.net/",
        "",
        "# The tiles a parent enabled are never blocked, whatever a list says.",
    ]
    lines += [f"server=/{host}/#" for host in sorted(set(allowed_hosts)) if host]
    lines += ["", "# SafeSearch: these names answer with the vendors' safe addresses."]
    for target, (names, _fallback) in SAFE_SEARCH.items():
        addresses = safe_search.get(target) or SAFE_SEARCH[target][1]
        lines.append(f"host-record={target},{','.join(addresses)}")
        lines.append(f"cname={','.join(names)},{target}")
    return "\n".join(lines) + "\n"


def render_nft(child_uids: Sequence[int], panel_port: int = PANEL_PORT) -> str:
    """The children's redirect, the DoH/DoT refusals and the closed panel, as one
    atomic nft file.

    The panel is closed here and not only in the browser. A URL list closes the
    spellings it names and nothing else - `127.0.0.1.nip.io:7878`, a tailnet
    address or a rebound name all reached the PIN page, where a child could
    trip the lockout and keep a parent out for a day. By uid, every program the
    child runs is refused every way in, and the parent's own browser is not
    touched.
    """
    uids = ", ".join(str(uid) for uid in sorted(set(child_uids)))
    elements = f"\n\t\telements = {{ {uids} }}" if uids else ""
    doh4 = ", ".join(DOH_ADDRESSES)
    doh6 = ", ".join(DOH_ADDRESSES6)
    return f"""# BioKids: children's DNS goes to the filter (D-167). Written by biokids-daemon.
destroy table inet {NFT_TABLE}
table inet {NFT_TABLE} {{
\tset children {{
\t\ttype uid{elements}
\t}}

\tchain dns_redirect {{
\t\ttype nat hook output priority -100; policy accept;
\t\tmeta skuid @children udp dport 53 redirect to :{DNS_PORT}
\t\tmeta skuid @children tcp dport 53 redirect to :{DNS_PORT}
\t}}

\tchain no_bypass {{
\t\ttype filter hook output priority 0; policy accept;
\t\tmeta skuid @children tcp dport 853 reject
\t\tmeta skuid @children udp dport 853 reject
\t\tmeta skuid @children ip daddr {{ {doh4} }} tcp dport 443 reject
\t\tmeta skuid @children ip daddr {{ {doh4} }} udp dport 443 reject
\t\tmeta skuid @children ip6 daddr {{ {doh6} }} tcp dport 443 reject
\t\tmeta skuid @children ip6 daddr {{ {doh6} }} udp dport 443 reject
\t\tmeta skuid @children tcp dport {panel_port} reject
\t\tmeta skuid @children ip daddr {TAILNET4} reject
\t\tmeta skuid @children ip6 daddr {TAILNET6} reject
\t}}
}}
"""


def uids_of(unix_users: Iterable[str]) -> list[int]:
    try:
        # Imported here: `pwd` exists only on POSIX, and the suite also runs on
        # the Windows development host.
        getpwnam = importlib.import_module("pwd").getpwnam
    except ImportError:
        return []
    found: list[int] = []
    for user in unix_users:
        try:
            found.append(getpwnam(user).pw_uid)
        except KeyError:
            log.warning("child account %s does not exist; its DNS is not filtered", user)
    return found


def write_atomically(path: Path, text: str) -> bool:
    """True when the content changed."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(text, encoding="utf-8")
    partial.chmod(0o600)
    partial.replace(path)
    return True


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class DnsFilter:
    """Writes the filter's files and puts them into effect. Never raises.

    Called on every daemon start and reload. The 300,000-line blocklist is
    rendered once per process and rewritten only when it changed; dnsmasq is
    restarted only when its configuration did, and nftables is reloaded every
    time, because something else (a `flush ruleset` from the system's own
    nftables service) may have removed it since.
    """

    def __init__(
        self, directory: Path, lists_dir: Path = LISTS_DIR, panel_port: int = PANEL_PORT
    ) -> None:
        self.files = FilterFiles(directory)
        self.lists_dir = lists_dir
        self.panel_port = panel_port
        self._blocklist: str | None = None
        self.last_error = ""

    def apply(self, child_users: Iterable[str], allowed_hosts: Iterable[str]) -> bool:
        # The redirect first and on its own: whatever else fails, the children's
        # DNS goes to the filter's port. If nothing answers there, they have no
        # internet; that is the fail-safe, never an unfiltered one (principle 3).
        try:
            write_atomically(self.files.nft, render_nft(uids_of(child_users), self.panel_port))
        except OSError as exc:
            self.last_error = f"files: {exc}"
            log.error("the children's DNS redirect could not be written: %s", exc)
            return False
        ok = _run(["nft", "-f", str(self.files.nft)])
        if not ok:
            self.last_error = "nftables"
            log.error("the children's DNS redirect could not be loaded")

        try:
            if self._blocklist is None:
                self._blocklist = render_blocklist(load_domains(self.lists_dir))
            changed = write_atomically(self.files.blocklist, self._blocklist)
            conf = render_conf(
                self.files, allowed_hosts=allowed_hosts, safe_search=resolve_safe_search()
            )
            changed = write_atomically(self.files.conf, conf) or changed
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            self.last_error = f"blocklist: {exc}"
            log.error("the DNS filter could not be prepared, it stays closed: %s", exc)
            _run(["systemctl", "stop", "--no-block", SERVICE])
            return False
        needs_start = changed or not _run(["systemctl", "is-active", "--quiet", SERVICE])
        if needs_start and not _run(["systemctl", "restart", "--no-block", SERVICE]):
            self.last_error = "dnsmasq"
            log.error("%s could not be (re)started", SERVICE)
            ok = False
        if ok:
            self.last_error = ""
        return ok

    def running(self) -> bool:
        return _run(["systemctl", "is-active", "--quiet", SERVICE])


def _run(argv: list[str]) -> bool:
    try:
        finished = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s could not be run: %s", argv[0], exc)
        return False
    if finished.returncode != 0 and argv[1:2] != ["is-active"]:
        log.warning(
            "%s exited %s: %s",
            " ".join(argv[:3]),
            finished.returncode,
            finished.stderr.decode("utf-8", "replace").strip()[:300],
        )
    return finished.returncode == 0
