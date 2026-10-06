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
| Closing or minimising the launcher | Chromium kiosk, no WM decorations, hotkeys suppressed |
| Opening a terminal | no terminal emulator installed, TTY switching disabled |
| Typing a blocked URL | Chromium policy + the children's DNS filter (D-167); the block page of D-64 is not built yet |
| Changing the DNS server in settings | settings app hidden; nftables sends the children's port 53 to the filter whatever server is set |
| Using a DoH-capable site or app | `DnsOverHttpsMode: off`; DoH hosts in the blocklist; the children's DoT and HTTPS to known DoH resolvers rejected |
| Asking the assistant something harmful | word list, classifier and a checked answer; a fixed refusal, and the parent is told (D-165) |
| A website opening the microphone or camera | `AudioCaptureAllowedUrls` names the launcher only; `VideoCaptureAllowed: false` (D-166) |
| Installing another browser | no sudo, no polkit, no Flatpak |
| Killing the daemon | no permission; crash triggers lockdown |
| Unplugging the network to escape the filter | fails closed: no DNS means no browsing |
| Creating a new user account | no sudo, `useradd` unavailable |
| Reading the parent's files | standard Unix permissions, separate home |
| Finding the parent panel | PIN gate plus URL blocked in the child profile |
| Guessing the PIN | argon2id, 5 attempts then 15-minute lockout, audited |

### Out of scope — document, do not pretend

These are stated plainly in the README:

1. **Booting from a USB stick or another OS.** Firmware territory. `biokids-harden`
   can set a GRUB password and disable recovery mode; BIOS/UEFI passwords and boot
   order are the parent's job and we document how.
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
| `biokids-voice` | child | audio only |
| `biokids-ai` | `biokids-ai` system user | runs the local models for the assistant (D-165); no network, no home, no devices |
| `biokids-dns` | started as root, `dnsmasq` drops to its own user | the children's DNS filter, on loopback port 5354 only (D-167) |
| `biokids-remote-setup` | root, oneshot, only when the parent asks | installs Tailscale and signs it in for phone access (D-169); apt needs root |

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
| `biokids-daemon` | `CAP_NET_ADMIN` (nft), `CAP_CHOWN` (its socket) | unix, netlink, inet, inet6 | `/var/lib/biokids /var/log/biokids /run/biokids /etc/chromium/policies/managed` |
| `biokids-panel` | none | unix, inet, inet6 | `/var/lib/biokids /var/log/biokids /run/biokids` |

The panel is root by uid only: the one root process a phone can reach holds no
capability a bug in it could use.

`biokids-ai` always runs with `PrivateNetwork=yes`: it has no reason to speak to the
internet and cannot. The one-time model download is a separate unit,
`biokids-ai-setup.service`, which only the parent's button starts, which has the
network and writes nothing but the models directory, and which checks every file
against its pinned SHA-256 (D-165).

---

## 3. Secrets

| Secret | Storage |
|---|---|
| Parent PIN | argon2id hash, `m=64MiB, t=3, p=4` |
| Parent passphrase | argon2id, same parameters |
| Recovery code | argon2id of the normalised 8-word phrase |
| SMTP password | encrypted with a key in `/etc/biokids/secret.key` (0600 root) |
| ElevenLabs API key | same |
| Extension ↔ daemon token | generated at install, `/etc/biokids/ext.token`, 0640 |
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
| ElevenLabs | cloud TTS | yes, opt-in, off by default, clearly labelled |

Zero telemetry. No crash reporting. No version ping. No analytics.

Remote access has two costs worth naming, both from Tailscale rather than BioKids
(D-157). Its client uploads diagnostic logs by default, which is why the runbook
turns that off. And `tailscale serve` needs HTTPS certificates enabled on the
tailnet, which puts the machine's `*.ts.net` name in the public Certificate
Transparency logs: the name becomes public, the panel does not.

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
}
```

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
per machine, so the strictest child's mode applies; the parent's panel is not in it
(nftables closes it to the children, above). The policy is
`biokids_core.browser_policy`.

```json
{
  "URLBlocklist": ["*", "file://*", "javascript://*", "chrome://settings", "..."],
  "URLAllowlist": ["127.0.0.1:7879", "eba.gov.tr", "trtcocuk.net.tr"],
  "AudioCaptureAllowed": false,
  "AudioCaptureAllowedUrls": ["http://127.0.0.1:7879"],
  "VideoCaptureAllowed": false,
  "DnsOverHttpsMode": "off",
  "ForceGoogleSafeSearch": true,
  "ForceYouTubeRestrict": 2,
  "IncognitoModeAvailability": 1,
  "DeveloperToolsAvailability": 2,
  "ExtensionInstallBlocklist": ["*"],
  "ExtensionInstallForcelist": ["<biokids-extension-id>;file:///usr/share/biokids/ext/update.xml"],
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PasswordManagerEnabled": false,
  "DownloadRestrictions": 3,
  "PrintingEnabled": false,
  "URLBlocklistExceptions": []
}
```

Note `DownloadRestrictions: 3` (block all downloads) for the 4–7 band; 8–10 gets `2`
(block dangerous). `ExtensionInstallBlocklist: ["*"]` combined with the forcelist means
our extension is the only one that can exist.

### Config regeneration must be atomic

```
render → temp file → fsync → rename → reload service → verify → commit
                                          │
                                          └─ failure → restore previous → lockdown → alert
```

Never leave a half-written filter config. A filter that silently fails open is the worst
possible failure in this project.

---

## 6. `biokids-harden`

Opt-in, reversible, never run automatically.

```
biokids-harden --check     # report current state, change nothing
biokids-harden --apply     # set GRUB password, disable recovery, disable TTY switch
biokids-harden --undo      # revert everything it did
```

`--apply` prints, before doing anything:

- the exact changes it will make
- the GRUB password it generated (once, for the parent to write down)
- the exact commands to undo them from a live USB if something goes wrong

If the parent cannot confirm they have recorded the password, it refuses to proceed.

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
- location tracking
- reading the contents of private messages

These are listed in `CONTRIBUTING.md` as out of scope, so the boundary is explicit
before anyone spends time on a pull request.

---

## 8. Reporting vulnerabilities

`SECURITY.md` in the repo root carries a short disclosure policy: a contact address,
a 90-day coordinated disclosure window, and a promise to credit reporters. Set this up
before the repository goes public.
