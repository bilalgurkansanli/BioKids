# SECURITY.md

Read before touching filtering, authentication, hardening, or anything that runs as
root.

---

## 1. Threat model

**The adversary is a curious child aged 4 to 10, working alone, with only this machine.**

That sentence is the whole model. It is stated in the README as well, because a
parental-control tool that implies more protection than it provides is worse than none.

### In scope

| Attack | Defence |
|---|---|
| Changing the system clock to get more time | `CLOCK_BOOTTIME` accounting (D-41) |
| Closing or minimising the launcher | Chromium kiosk, no WM decorations, hotkeys suppressed; the app bar is never shown over it and acts only on app windows (D-197) |
| Using the app bar's close to stop the launcher or Zio and Sio | it stops only a `biokids-app-*.scope`, checked by name and by its cgroup path under the child's own `user@<uid>.service`; a window claiming another process's `_NET_WM_PID` can at most stop another of the child's own apps (D-197) |
| Opening a terminal | no terminal emulator installed, TTY switching disabled |
| Typing a blocked URL | Chromium policy + the children's DNS filter (D-167); the extension turns the error into the launcher's block page (D-187) |
| Searching for adult words (Google, YouTube, Wikipedia, the offline Vikipedi, the address bar) | the extension's `declarativeNetRequest` rules refuse the request before it is sent, suggestions included; the address bar searches SafeSearch Google and suggests nothing (D-202, section 5) |
| Removing or switching off the BioKids extension | force-installed by policy, `chrome://extensions` closed, every other extension blocked (D-187) |
| Opening a sibling's launcher, or posting to it | the `launchers` nftables chain: a launcher port accepts only its own child's uid (D-188) |
| Changing the DNS server in settings | settings app hidden; nftables sends the children's port 53 to the filter whatever server is set |
| Using a DoH-capable site or app | `DnsOverHttpsMode: off`; DoH hosts in the blocklist; the children's DoT and HTTPS to known DoH resolvers rejected |
| Asking the assistant something harmful | word list, classifier, and every sentence of the answer checked before it is shown (D-216); a fixed refusal, and the parent is told (D-165) |
| A website opening the microphone or camera | `AudioCaptureAllowedUrls` names the launcher only; `VideoCaptureAllowed: false` (D-166) |
| Installing another browser | no sudo, no polkit, no Flatpak |
| Killing the daemon | no permission; crash triggers lockdown |
| Unplugging the network to escape the filter | fails closed: no DNS means no browsing |
| Creating a new user account | no sudo, `useradd` unavailable |
| Reading the parent's files | standard Unix permissions, separate home |
| Finding the parent panel | PIN gate; nftables rejects the children's connections to the panel's port and the tailnet (D-171) |
| Guessing the PIN | argon2id, 5 attempts then 15-minute lockout, audited |

### Out of scope — document, do not pretend

These are stated plainly in the README:

1. **Booting from a USB stick or another OS.** Firmware territory. A UEFI/BIOS
   password and a GRUB password are the parent's job; section 6 says how, by hand.
2. **Editing the kernel command line** (`init=/bin/bash`). Mitigated only by a GRUB
   password, which is opt-in because it locks out parents too.
3. **Removing the drive**, or mounting it from another machine. Full-disk encryption
   would help and is not in v1.
4. **A second device.** BioKids protects one computer. It does not protect a phone,
   a tablet, or a friend's laptop.
5. **A technically capable teenager, or an adult helping the child.** Explicitly not
   the target. If you need that, you need a different class of product.
6. **Physical access to the parent's unlocked session.**

Never add a claim to marketing copy that contradicts this list.

---

## 2. Privilege model

| Component | User | Why |
|---|---|---|
| `biokids-daemon` | root | needs logind control, nftables, service reloads, shutdown |
| `biokids-panel` | root | writes config that only root may write |
| `biokids-shell` | child | runs in the child session; no privilege. Reaches the daemon only over `/run/biokids/shell.sock`, where every operation is scoped to the calling uid's own child (D-123) |
| the app bar (`biokids_shell.appbar`) | child, a child process of `biokids-shell` | reads windows through libwnck and maximises an app window that does not fill the screen (D-201, D-205), steps the volume of the active app's own sound streams (`pactl`), and stops an app that would not close with `systemctl --user stop` - only ever a `biokids-app-*.scope` in the child's own user manager, never the launcher's or the voice's unit (D-197). No privilege: nothing it does is beyond what the child's own session may do |
| `biokids-voice` | child | audio only |
| `biokids-ai` | `biokids-ai` system user | runs the local models for the assistant (D-165); no network, no home, no devices |
| `biokids-wiki` | `biokids-ai` system user | serves the offline Wikipedia on `127.0.0.1:7877` once a parent downloaded it (D-190); no capabilities, `IPAddressDeny=any` except localhost |
| `biokids-dns` | started as root, `dnsmasq` drops to its own user | the children's DNS filter, on loopback port 5354 only (D-167) |
| `biokids-remote-setup` | root, oneshot, only when the parent asks | installs Tailscale and signs it in for phone access (D-169); apt needs root |
| `biokids-voice-setup` | root by uid only, oneshot, only when the parent asks | downloads Piper and its Turkish voice into `/var/lib/biokids-voices`, each file pinned by SHA-256 (D-196); no capability, nothing else writable |
| `biokids-provision@` | root, oneshot, one instance per child added | creates the child's account and session files: one fixed command, `biokids-admin provision-child %I`, because the panel's sandbox cannot write `/etc` or `/home` (D-142) |
| `biokids-unprovision@` | root, oneshot, one instance per child removed | the reverse, `biokids-admin unprovision-child %I`: out of `biokids-child`, `SystemAccount=true` for AccountsService and a `nologin` shell, only BioKids' own session files removed; the account and its home stay (D-191) |
| the BioKids extension | inside Chromium, every profile on the machine | `webNavigation`, `storage`, `alarms` and `declarativeNetRequest` (block and allow only, D-202), host permission `http://127.0.0.1/*` only; no content scripts, no `tabs` (D-187) |

Root services run with systemd hardening:

```ini
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
LockPersonality=yes
RestrictRealtime=yes
SystemCallArchitectures=native
ProtectClock=yes
ProtectKernelLogs=yes
ProtectHostname=yes
```

On top of that block, each root unit names the little of root it keeps (D-171):

| Unit | `CapabilityBoundingSet=` | `RestrictAddressFamilies=` | `ReadWritePaths=` |
|---|---|---|---|
| `biokids-daemon` | `CAP_NET_ADMIN` (nft), `CAP_CHOWN` (its socket) | unix, netlink, inet, inet6 | `/var/lib/biokids /run/biokids /etc/chromium/policies/managed` |
| `biokids-panel` | none | unix, inet, inet6 | `/var/lib/biokids /run/biokids /var/lib/biokids-voices` |
| `biokids-voice-setup` | none | unix, inet, inet6 | `/var/lib/biokids-voices` |

The panel is root by uid only: the one root process a phone can reach holds no
capability a bug in it could use.

`biokids-ai` always runs with `PrivateNetwork=yes`: it has no reason to speak to the
internet and cannot. The one-time model download is a separate unit,
`biokids-ai-setup.service`, which only the parent's button starts, which has the
network and writes nothing but the models directory, and which checks every file
against its pinned SHA-256 (D-165).

The voice service has no network either; it runs as the child and only plays and
synthesises. The natural voice's one-time download is `biokids-voice-setup.service`,
started only by the parent's button, with the network, no capability and nothing
writable but `/var/lib/biokids-voices`. It refuses any file whose size or SHA-256 is
not the pinned one, and unpacks Piper's archive with tarfile's `data` filter, only
under the archive's own `piper/` (D-196).

---

## 3. Secrets

| Secret | Storage |
|---|---|
| Parent PIN | argon2id hash, `m=64MiB, t=3, p=4` |
| Parent passphrase | argon2id, same parameters |
| Recovery code | argon2id of the normalised 8-word phrase |
| SMTP password | encrypted with a key in `/etc/biokids/secret.key` (0600 root), with the rest of the mail settings (D-177) |
| Browser extension signing key | generated on the machine at install, `/var/lib/biokids/extension-key.pem`, 0600; never in the repository (D-187) |
| APT repo signing key | GitHub Secrets only. Never in the repo, never on the device |

No secret is ever logged, echoed in an error message, or included in a data export.
The export feature explicitly redacts them.

---

## 4. Outbound network inventory

Principle 1 says nothing leaves the device. That is only meaningful if the exceptions
are enumerated. **This table is the complete list. Adding a row requires a decision
entry in `DECISIONS.md` and a note in the README.**

| Destination | Purpose | Optional? |
|---|---|---|
| `deb.debian.org`, `security.debian.org` | OS updates | no |
| our APT repo | BioKids updates | no |
| the configured DNS upstream | resolution | no (can be a LAN resolver) |
| blocklist source URLs | weekly refresh | **not built**: the lists ship inside biokids-core and update with it (D-167) |
| the parent's own SMTP server | notifications | yes, opt-in |
| Tailscale coordination | remote panel access | yes, opt-in |
| `pkgs.tailscale.com` | the Tailscale package and its updates, through apt, signed by the key BioKids ships | yes: only after the parent presses "Telefondan erişimi aç" on a machine without Tailscale (D-169) |
| `log.tailscale.com` | Tailscale's own client logs | **turned off**: the runbook starts `tailscaled` with `--no-logs-no-support` (D-157) |
| Hugging Face | one-time download of the assistant's models | yes: only with the AI package, only when the parent presses the button (D-165) |
| `download.kiwix.org` and the mirror it redirects to | one-time download of the encyclopedia the assistant checks itself against, pinned by SHA-256 | yes: the same button, the same package (D-178) |
| `github.com` and `release-assets.githubusercontent.com`, where it redirects | one-time download of the Piper program, release 2023.11.14-2, pinned by SHA-256 | yes: only when the parent presses "Doğal sesi indir" (D-196) |
| `huggingface.co` and its CDN (`*.cdn.hf.co`), where it redirects | one-time download of Piper's Turkish voice, pinned to a commit and by SHA-256 | yes: the same button (D-196) |

Zero telemetry. No crash reporting. No version ping. No analytics.

The browser extension (D-187) adds no row. It connects only to the child's own launcher
on `127.0.0.1`, its update manifest is a `file://` path on the machine, and the host
names it reports go into the family's database and nowhere else. Its search rules
(D-202) come from the same launcher, and a search they refuse is never sent at all. The offline Wikipedia
(D-190) adds none either: `biokids-wiki` serves the files the assistant's download
already fetched, on loopback, under `IPAddressDeny=any`.

Remote access has two costs worth naming, both from Tailscale rather than BioKids
(D-157). Its client uploads diagnostic logs by default, which is why the runbook
turns that off. And `tailscale serve` needs HTTPS certificates enabled on the
tailnet, which puts the machine's `*.ts.net` name in the public Certificate
Transparency logs: the name becomes public, the panel does not.

### What stays on the machine, and for how long

Nothing leaving the device is half of principle 1; the other half is not keeping more
than the family needs. The daemon deletes what is past its window once a day (D-189),
from `[retention]` in `/etc/biokids/config.toml`:

| Records | Key | Default |
|---|---|---|
| usage events, which include the sites visited, the assistant's alerts, and the hours of the day the computer was used (`usage_hour`, D-206) | `activity_days` | 30 days |
| minutes per day and per app (`usage_tick`, `ledger_day`) | `usage_days` | 400 days |
| the audit log | `audit_days` | 365 days |
| the children's requests | `requests_days` | 90 days |

The insights screen and the Sunday mail's highlights (D-206) are computed from these
when they are read and store nothing of their own; the mail names no site.
Panel sessions are deleted a day after their idle timeout, and only the 20 newest boot
epochs are kept. Removing a child deletes everything recorded for them except the audit
log (D-191); purging `biokids-core` deletes the database, if the parent says yes.

---

## 5. Filtering internals

### Forcing DNS

Only the children are filtered (D-167). The daemon renders this from the children's
uids on every reload and loads it with `nft -f`, which applies it as one transaction:

```
# /var/lib/biokids/dns/biokids.nft  (rendered, do not hand-edit)
destroy table inet biokids
table inet biokids {
  set children { type uid; elements = { 1001, 1002 } }

  chain dns_redirect {
    type nat hook output priority -100; policy accept;
    meta skuid @children udp dport 53 redirect to :5354
    meta skuid @children tcp dport 53 redirect to :5354
  }

  chain no_bypass {
    type filter hook output priority 0; policy accept;
    meta skuid @children tcp dport 853 reject
    meta skuid @children udp dport 853 reject
    meta skuid @children ip daddr { 1.1.1.1, 8.8.8.8, 9.9.9.9, ... } tcp dport 443 reject
    # ... and the same for UDP and for the resolvers' IPv6 addresses
    meta skuid @children tcp dport 7878 reject           # the parent's panel
    meta skuid @children ip daddr 100.64.0.0/10 reject   # the tailnet
    meta skuid @children ip6 daddr fd7a:115c:a1e0::/48 reject
  }

  chain launchers {
    type filter hook output priority 0; policy accept;
    ip daddr 127.0.0.1 tcp dport 7880 meta skuid != 1001 reject   # 7879 + 1001 % 100
    ip6 daddr ::1 tcp dport 7880 meta skuid != 1001 reject
    ip daddr 127.0.0.1 tcp dport 7881 meta skuid != 1002 reject
    ip6 daddr ::1 tcp dport 7881 meta skuid != 1002 reject
  }
}
```

Each child's launcher listens on a port of its own, `shell.port + uid % 100` (D-188),
and answers as that child on the daemon's socket. The `launchers` chain lets nobody
else connect to it: not a sibling logged in at the same time, not the parent, not a
page in anyone's browser. The extension, which the machine-wide policy installs in the
parent's Chromium too, finds no launcher there and does nothing.

The panel is closed here, by uid, rather than in the browser (D-171). A URL list
closes the spellings it names: `127.0.0.1.nip.io:7878`, the machine's tailnet
address or a rebound name all reached the PIN page, where a child could trip the
lockout and keep a parent out for a day. And because Chromium's policy is
machine-wide, every address it closed was closed in the parent's own browser too.
dnsmasq also runs with `stop-dns-rebind`: no public name may answer with a private
or loopback address for a child.

Port 5354 is `dnsmasq`, run by `biokids-dns.service` from `dnsmasq-base` with a
configuration the daemon writes to `/var/lib/biokids/dns/`: loopback only, the
blocklists as `local=/domain/` (NXDOMAIN for the domain and every subdomain),
SafeSearch as `cname` and `host-record` lines, and each site a parent enabled as
`server=/host/#` so a list can never close it. DoH hosts are one of the lists, and
`use-application-dns.net` answers NXDOMAIN so Firefox would not switch DoH on.

The redirect is written and loaded before anything else, and on its own. If the
lists cannot be read, the filter is stopped; if it crashes, systemd restarts it. In
both cases the children's DNS goes to a port where nothing answers, so they have no
internet rather than unfiltered internet. On removal, `biokids-daemon.prerm` deletes
the table.

### Allowlist mode

"Only the sites I choose". The open mode is the same policy without the `*`:
`file://`, typed `javascript:` and Chromium's own pages stay blocked in both, and
certificate warnings cannot be clicked through. Chromium reads one managed policy
per machine, so the strictest child's mode applies, in the parent's Chromium too. The
parent's panel (`127.0.0.1:7878`, `localhost:7878`) is therefore allowed in it
(D-195): with only `*` it closed the panel the menu and the desktop shortcut open,
and nftables already closes the port to every child (above). The policy is
`biokids_core.browser_policy`.

```json
{
  "URLBlocklist": ["*", "file://*", "javascript://*", "chrome://settings", "..."],
  "URLAllowlist": ["127.0.0.1:7880", "127.0.0.1:7881", "eba.gov.tr", "trtcocuk.net.tr"],
  "AudioCaptureAllowed": false,
  "AudioCaptureAllowedUrls": ["http://127.0.0.1:7880", "http://127.0.0.1:7881"],
  "VideoCaptureAllowed": false,
  "DnsOverHttpsMode": "off",
  "ForceGoogleSafeSearch": true,
  "ForceYouTubeRestrict": 2,
  "DefaultSearchProviderEnabled": true,
  "DefaultSearchProviderSearchURL": "https://www.google.com/search?q={searchTerms}",
  "SearchSuggestEnabled": false,
  "NetworkPredictionOptions": 2,
  "IncognitoModeAvailability": 1,
  "DeveloperToolsAvailability": 2,
  "ExtensionInstallBlocklist": ["*"],
  "ExtensionInstallForcelist": ["<extension-id>;file:///var/lib/biokids-ext/updates.xml"],
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PasswordManagerEnabled": false,
  "DownloadRestrictions": 3,
  "PrintingEnabled": false,
  "URLBlocklistExceptions": []
}
```

`DownloadRestrictions: 3` blocks every download, for every child: there is one policy
for the machine. `ExtensionInstallBlocklist: ["*"]` combined with the forcelist means
our extension is the only one that can exist.

### The extension

`ExtensionInstallForcelist` names the BioKids extension (D-187) only once it has been
packed. biokids-shell's postinst runs `biokids-admin pack-extension`, which signs it as
a CRX3 with a key made on this machine, `/var/lib/biokids/extension-key.pem` (0600, in
the 0700 data directory, never in the repository or a package), and writes
`biokids.crx`, `updates.xml` and `extension-id` into `/var/lib/biokids-ext` (0755, so
the child's Chromium can read them). The id comes from the key, so nothing off the
machine can sign an update Chromium would accept.

What it can see is the address of each top-level navigation in a tab's main frame,
through `webNavigation`. What it sends is the host name, `{"host": "…"}`, to the
child's own launcher at `POST /visit`, at most once per host per ten minutes; the
launcher accepts a host name and nothing else, and passes it to the daemon over
`shell.sock`. A navigation closed by the policy or the DNS filter is sent to the
launcher's `/blocked` page. It has no content scripts and no `tabs` permission, so it
cannot read a page, a title or a form, and it never reports an address on the machine.

### Searches (D-202)

The DNS filter closes sites; it cannot close a question asked of a site it allows. So
the extension also installs `declarativeNetRequest` dynamic rules that refuse a request
whose address carries a word on BioKids' list, before it leaves the machine: the
search engine never sees it.

- **The rules** are regular expressions from `biokids_core.search_filter`, served by
  the child's launcher at `GET /search-rules`: the default list
  (`biokids_core/data/search_words.toml`, which says what it covers and what it leaves
  out) and the words the parent added under Çocuklar → İnternet → Gelişmiş, one list
  for the family. The extension checks each with `isRegexSupported` and builds the
  rules itself: rule 1 *allows* the launcher's own origin, every other rule *blocks*;
  action, priority and resource types (`main_frame`, `sub_frame`, `xmlhttprequest`,
  `script`) are fixed in `background.js`, never taken from the answer. Blocking needs
  no host permission; a redirect would need one on every site. The launcher refuses
  `/search-rules` as a page and leaves the parent's words out of `/state`: its address
  is open in the child's browser.
- **What matches.** A term at the start of a word. A short one (five letters or fewer)
  only in a search box's value - `q`, `query`, `search`, `search_query`, `text`,
  `pattern`, `term`: Google, Bing, DuckDuckGo, YouTube, Wikipedia, Yandex, kiwix-serve,
  and the suggestions each fetches while the child types - or in a title after
  `/wiki/` or `/content/`. A longer one, or a phrase, after any `=` or `/`. Each letter
  in any case, a Turkish letter also as its ASCII neighbour and its capital, look-alike
  digits, held-down letters; a dotted "i" never as a dotless "ı".
- **The page.** A refused page or frame fails with `ERR_BLOCKED_BY_CLIENT`; the
  extension sends the tab to `/blocked?reason=search.blocked`, which says "Bunu
  arayamayız. İstersen bir büyüğüne sorabilirsin." and offers "Büyüğüme sor". The word
  is in neither: the launcher is told why, never what, and the request it files names
  nothing. A page that searches without loading one (YouTube's in-page search, which
  posts the words and then changes the address) is checked when its address changes.
- **Fail safe.** The whole set is swapped in one `updateDynamicRules` call; dynamic
  rules outlive a browser restart, so a launcher that does not answer leaves the last
  set in force, and a set built without the parent's words never replaces one built
  with them. No rules at all is logged as an error in the extension's console. The
  launcher serves the default list even when the daemon is away.
- **The address bar** searches Google (`DefaultSearchProvider*`), where SafeSearch is
  forced and the rules see the search like any other. Its suggestions are off
  (`SearchSuggestEnabled: false`): the browser fetches them itself, out of every
  extension's sight. Nothing is preloaded (`NetworkPredictionOptions: 2`). One policy
  for the machine, so the parent's Chromium searches the same way.
- **Limits.** Chromium gives each expression 2 KB of RE2 memory and an extension at
  most 1000 of them. `search_filter.re2_cost` estimates a pattern's instructions and
  terms are packed up to `MAX_COST` (104); every pattern built at that budget and at
  116 was accepted by Chromium 154. A form that posts its search, the words inside
  YouTube's in-page search request, and a spelled-out word in a suggestion written
  with `%20` are not seen. A determined child with a second browser is out of scope
  (section 1).

### Config regeneration must be atomic

```
render → temp file → fsync → rename → reload service → verify → commit
                                          │
                                          └─ failure → restore previous → lockdown → alert
```

Never leave a half-written filter config. A filter that silently fails open is the worst
possible failure in this project.

---

## 6. Locking the machine itself (by hand)

BioKids does not change the bootloader or the firmware (D-184): a tool that sets a
GRUB password can lock a parent out of their own computer, and principle 4 comes
first. A family that wants the out-of-scope attacks of section 1 harder can do it
themselves, knowing how to undo it:

- **Firmware:** set a UEFI/BIOS administrator password and put the internal disk
  first in the boot order. Write the password down; a forgotten one may need the
  manufacturer.
- **GRUB:** `grub-mkpasswd-pbkdf2`, then a `password_pbkdf2` line and
  `set superusers` in `/etc/grub.d/40_custom`, and `update-grub`. Recovery entries
  can be hidden with `GRUB_DISABLE_RECOVERY=true` in `/etc/default/grub`. To undo:
  remove the lines and run `update-grub` again, from a live USB if need be.
- **Disk encryption** is chosen when Debian is installed, not afterwards.

The virtual-terminal guards BioKids does ship (D-136) are packaged defaults in
`/usr/lib/systemd/logind.conf.d/` and `/usr/share/X11/xorg.conf.d/` and go away with
the package.

---

## 7. Responsible use

The project ships with a statement, in both languages, in the README and the wizard:

> BioKids is a supervision aid, not a guarantee. No filter catches everything. The
> supervision features exist to start conversations with your child, not to replace
> them. Tell your child what the computer does and why.

We do not build, and will not accept contributions for:

- covert monitoring, hidden modes, or a "stealth" install
- keystroke logging
- microphone or camera capture, with one exception (D-166): push-to-talk in the
  assistant, off by default, only while the child holds the button and for at most
  15 seconds, usable by the launcher alone, transcribed on the machine and never
  saved or sent. Background listening, wake words and keeping audio stay out
  of scope.
- live screen mirroring of a child's session
- a record of the pages a child reads: the extension reports a site's host name,
  never its path, query, title or contents, and the child is told on their first
  run (D-187)
- a record of what a child searches: a refused search is not reported or kept, not
  even the fact that it happened, unless the child presses "Büyüğüme sor" - and that
  request names nothing (D-202)
- location tracking
- reading the contents of private messages

These are listed in `CONTRIBUTING.md` as out of scope, so the boundary is explicit
before anyone spends time on a pull request.

---

## 8. Reporting vulnerabilities

`SECURITY.md` in the repo root carries a short disclosure policy: a contact address,
a 90-day coordinated disclosure window, and a promise to credit reporters. Set this up
before the repository goes public.
