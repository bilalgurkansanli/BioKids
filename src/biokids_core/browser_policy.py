"""The managed Chromium policy for the children's browser (SECURITY.md section 5).

In `biokids_core` rather than in the launcher because the daemon writes it: the
daemon is the process that runs as root and may write
`/etc/chromium/policies/managed`, and `biokids-daemon` does not depend on
`biokids-shell`. It used to import this from the launcher's package and failed
to start on a machine that had the one without the other.

The parent's panel is not in it. Chromium reads one policy for the whole
machine, so every address a child must not open was closed in the parent's own
Chromium too - including the panel the applications menu opens (D-170). The
panel is closed to the children's accounts by nftables instead, by uid
(`biokids_daemon.dnsfilter.render_nft`), which also covers every spelling a
URL list misses: `127.0.0.1.nip.io:7878`, a tailnet address, a rebound name.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

MANAGED_POLICY_DIR = Path("/etc/chromium/policies/managed")
"""The only place Chromium on Linux reads a managed policy from, and the default
for `PathsConfig.chromium_policy_dir`.

Not `<user-data-dir>/policies/managed/`, which is where this file used to write
one: `--user-data-dir` adds no policy source, so every lockdown T5.2 promises
was inert and `chrome://policy` in the kiosk profile was empty.

The daemon writes the file - it runs as root and its unit carries this directory
in `ReadWritePaths` - and the launcher, which runs as the child, only checks that
it is there."""

POLICY_FILENAME = "biokids-kiosk.json"

INTERNAL_PAGES = (
    "chrome://settings",
    "chrome://flags",
    "chrome://extensions",
    "chrome://net-internals",
    "chrome://inspect",
    "chrome://downloads",
    "chrome://password-manager",
    "chrome://about",
    "chrome://chrome-urls",
    "chrome://policy",
    "chrome://management",
    "chrome://version",
    "chrome://system",
    "chrome://history",
)
"""Chromium's own pages: its settings, and the pages that list the rest or show
what the policy closes."""

BLOCKED_SCHEMES = ("file://*", "javascript://*")
"""The disk, through `Ctrl+O` or a typed path, and script typed into the
address bar. Neither has anything for a child; both are ways around the rest."""


EXTENSION_ID_FILE = "extension-id"
UPDATES_FILE = "updates.xml"


def packed_extension(extension_dir: Path) -> tuple[str, str] | None:
    """(id, update URL) of the extension packed on this machine, or None (D-187)."""
    try:
        ext_id = (extension_dir / EXTENSION_ID_FILE).read_text(encoding="ascii").strip()
    except OSError:
        return None
    if len(ext_id) != 32 or not all("a" <= ch <= "p" for ch in ext_id):
        return None
    return ext_id, (extension_dir / UPDATES_FILE).as_uri()


def kiosk_policy(
    origin: str | Sequence[str],
    web_hosts: Sequence[str] = (),
    mode: str = "allowlist",
    extension: tuple[str, str] | None = None,
) -> dict[str, object]:
    """The managed policy for the kiosk, and for the child's browsing window.

    Everything here is a `SECURITY.md` §5 setting. Chromium on Linux reads
    managed policy only from `/etc/chromium/policies/managed`, for every
    profile on the machine at once - a profile directory adds no policy source
    (D-149) - so this one file governs the launcher *and* the window a web tile
    opens. With only the launcher's origin allowed, every web tile a parent
    enabled opened on "This page is blocked" (D-156).

    So the allowlist is the launcher's origin plus `web_hosts`: the hosts of
    the web tiles enabled for any child, which the daemon collects. A host with
    no scheme allows it over http and https and allows its subdomains, which
    is what a site that sends you from `eba.gov.tr` to `giris.eba.gov.tr` needs.
    "Type another address" still has nowhere to go.
    """
    # One origin per child's launcher (D-188), each reachable only by its own
    # child's processes (nftables).
    origins = [origin] if isinstance(origin, str) else list(origin)
    allowed = [*origins, *sorted({h for h in web_hosts if h and h not in origins})]
    # In "open internet" mode (D-167) there is no wildcard: harmful sites are
    # stopped by the children's DNS filter, and the browser keeps only what
    # must never open for a child - the disk, typed script, and Chromium's own
    # pages, which `*` stopped covering in Chromium 147. The parent's panel is
    # closed by nftables, not here (see the module's docstring).
    closed = [*BLOCKED_SCHEMES, *INTERNAL_PAGES]
    return {
        "URLBlocklist": ["*", *closed] if mode == "allowlist" else closed,
        "URLAllowlist": allowed,
        "ForceGoogleSafeSearch": True,
        "ForceYouTubeRestrict": 2,
        "ForceBingSafeSearch": 2,
        "URLBlocklistExceptions": [],
        "DeveloperToolsAvailability": 2,
        "IncognitoModeAvailability": 1,
        "ExtensionInstallBlocklist": ["*"],
        "BrowserSignin": 0,
        "SyncDisabled": True,
        "PasswordManagerEnabled": False,
        "DownloadRestrictions": 3,
        "PrintingEnabled": False,
        "DefaultPopupsSetting": 2,
        "DefaultNotificationsSetting": 2,
        "DefaultGeolocationSetting": 2,
        "TranslateEnabled": False,
        "ShowHomeButton": False,
        "BookmarkBarEnabled": False,
        "EditBookmarksEnabled": False,
        "AutofillAddressEnabled": False,
        "AutofillCreditCardEnabled": False,
        "DnsOverHttpsMode": "off",
        # A certificate warning is not something to click through: the panel
        # over Tailscale, a captive portal or an interception all start there.
        "SSLErrorOverrideAllowed": False,
        "BrowserGuestModeEnabled": False,
        "BrowserAddPersonEnabled": False,
        # The microphone: the launcher's own page may use it - for press-and-
        # speak, and only while the child holds the button (D-166) - and no
        # site, app page or extension may. No prompt either way.
        "AudioCaptureAllowed": False,
        "AudioCaptureAllowedUrls": [f"http://{o}" for o in origins],
        "VideoCaptureAllowed": False,
        # The BioKids extension (D-187): block page and visited hosts. Force-
        # installed, so it is exempt from the blocklist above and cannot be
        # turned off.
        **(
            {"ExtensionInstallForcelist": [f"{extension[0]};{extension[1]}"]}
            if extension
            else {}
        ),
    }
