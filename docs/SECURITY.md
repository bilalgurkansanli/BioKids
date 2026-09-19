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
| Typing a blocked URL | Chromium policy + local DNS + block page |
| Changing the DNS server in settings | settings app hidden; nftables forces port 53 to local |
| Using a DoH-capable site or app | `DnsOverHttpsMode: off`, bootstrap hosts dropped |
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
| `biokids-ai` | `biokids-ai` system user | needs X11 access for capture, nothing else |

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
ReadWritePaths=/var/lib/biokids /var/log/biokids /run/biokids /etc/dnsmasq.d \
               /etc/chromium/policies/managed /etc/nftables.d
```

`biokids-ai` additionally gets `PrivateNetwork=yes` once the model is downloaded. It
has no reason to speak to the internet and must not be able to.

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
| blocklist source URLs | weekly refresh | yes, opt-in |
| the parent's own SMTP server | notifications | yes, opt-in |
| Tailscale coordination | remote panel access | yes, opt-in |
| Hugging Face | one-time model download | yes, only with the AI package |
| ElevenLabs | cloud TTS | yes, opt-in, off by default, clearly labelled |

Zero telemetry. No crash reporting. No version ping. No analytics.

---

## 5. Filtering internals

### Forcing DNS

```
# /etc/nftables.d/biokids.nft  (rendered, do not hand-edit)
table inet biokids {
  chain output {
    type nat hook output priority -100;
    # allow the local resolver itself
    meta skuid dnsmasq accept
    # everything else: port 53 and 853 go to the local resolver
    udp dport 53  redirect to :53
    tcp dport 53  redirect to :53
    tcp dport 853 drop
  }
}
```

DoH bootstrap endpoints (`mozilla.cloudflare-dns.com`, `dns.google`,
`chrome.cloudflare-dns.com`, and the rest of the current list) are resolved to
`0.0.0.0` in dnsmasq. Keep the list in `/usr/share/biokids/doh-hosts.txt` and refresh
it with the blocklists.

### Allowlist mode

```json
{
  "URLBlocklist": ["*"],
  "URLAllowlist": ["eba.gov.tr", "trtcocuk.net.tr", "127.0.0.1:7879"],
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
- microphone or camera capture
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
