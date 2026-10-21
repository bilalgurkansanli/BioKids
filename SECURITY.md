# Security policy

BioKids runs as root on a family's computer and holds a record of how a child spends
their time. A weakness in it is worth reporting carefully, and it will be treated
seriously.

This file is the disclosure policy. The threat model — what BioKids defends against and
what it openly does not — is [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Reporting a vulnerability

Report privately. Do not open a public issue, a discussion, or a pull request that
demonstrates the problem.

Two channels, either is fine:

- **GitHub private vulnerability report** —
  <https://github.com/bilalgurkansanli/BioKids/security/advisories/new>.
  Preferred: the report, the fix and the advisory stay in one place.
- **Email** — <bilalsanli129@gmail.com>

Useful to include, as far as you have it:

- the BioKids version (`apt policy biokids`, or the version at the bottom of the panel)
- the Debian version, and whether the machine is a VM or real hardware
- which component: daemon, panel, shell, voice, ai, packaging
- what an attacker can actually do, in one sentence
- the steps to reproduce

A proof of concept helps a lot. A working exploit is welcome and will never be published
without your agreement.

---

## What not to send

Redact before you attach anything. This project exists to keep a child's data on their
own machine, and a vulnerability report is not an exception to that.

- **No screenshots of a child's screen.**
- **No child's name**, school, photo, or anything else that identifies them.
- **Nothing from `/var/lib/biokids/evidence/`.** Those are flagged frames from a real
  session. We do not want them, cannot store them, and will not look at them.
- **No database from a real installation.** If reproducing needs specific data, describe
  the rows, or build the case on a test install.

A report that contains this material will still be answered — but you will be asked to
send it again without, and the original will be deleted.

---

## Response

One maintainer, a hobby project, no bounty programme. What is promised instead is a
timetable that is actually achievable:

| | |
|---|---|
| Acknowledgement that the report arrived | within 7 days |
| First assessment: severity, in scope or not, and a plan | within 30 days |
| A fix, or a documented mitigation parents can apply | before day 90 |
| Public advisory | when the fix ships, or day 90 — whichever comes first |

**Coordinated disclosure, 90 days.** If a fix ships earlier, the advisory goes out
earlier. If you have a reason to want a different window — shorter because it is being
exploited, longer because a coordinated fix needs it — say so and it will be agreed
rather than imposed. If day 90 arrives and there is no fix, publishing is your call to
make; an unfixed hole that nobody knows about is worse for families than a public one.

---

## Credit

Reporters are credited by name or handle in the advisory and in `CHANGELOG.md` under
Security, unless they would rather not be. Say how you want to be named, or that you
want to stay anonymous, and that is how it will be done.

---

## Supported versions

Pre-1.0. Only the most recent release in the APT repository's `trixie` distribution
receives fixes. `trixie-testing` is the development channel and carries no promises.
This policy is revisited at `1.0.0`.

---

## Scope

The adversary BioKids is built against is **a curious child aged 4 to 10, working alone,
with only this machine**. That sentence is the whole threat model; the full version, with
the defences and the documented gaps, is [`docs/SECURITY.md` §1](docs/SECURITY.md#1-threat-model).

### In scope

- Privilege escalation from the child's account — or from the `biokids-ai` system user —
  to root, or to the parent's account.
- An unprivileged local user getting commands accepted by `/run/biokids/daemon.sock`, or
  by any other BioKids IPC surface.
- Defeating the parent gate: the argon2id PIN or passphrase, the five-attempt lockout,
  the signed session cookie, the idle timeout, or the recovery code.
- Defeating time enforcement **from inside the child's session** — not by rebooting into
  something else. Clock manipulation, session tricks, and ledger corruption all count.
- Escaping the launcher into a general-purpose machine from inside the child's session: a
  shell, a terminal emulator, a file manager, a second browser, the settings application,
  a virtual-terminal switch, or a Chromium window that is no longer a kiosk.
- Defeating the filtering *mechanism* rather than finding a gap in a list: getting DNS
  answered by something other than the local resolver, reaching a DoH endpoint that is
  supposed to be dropped, or loading an extension when the policy allows only ours. A
  site the blocklist simply never knew about is the separate, out-of-scope case below.
- Failing open: the daemon, the filter or the policy engine continuing unprotected after
  a crash or a bad config, instead of locking the session and alerting the parent.
- A secret reaching somewhere it should not — the SMTP password, an API key, the
  extension token, or a password hash appearing in a log, an error message, an audit
  entry or a data export.
- The parent panel becoming reachable from outside the machine when the parent did not
  turn on remote access.
- A maintainer script that damages a system, or a package `remove` that destroys data it
  promised to keep until `purge`.
- Any outbound connection that is not in the inventory at
  [`docs/SECURITY.md` §4](docs/SECURITY.md#4-outbound-network-inventory).
- A lockout with no documented recovery path — including anything `biokids-harden` can
  leave behind.

### Out of scope

These are documented limits, not bugs. They are listed in the README and in
[`docs/SECURITY.md` §1](docs/SECURITY.md#1-threat-model), and BioKids does not claim to
stop them:

1. Booting from a USB stick or another operating system.
2. Editing the kernel command line (`init=/bin/bash`). A way to bypass a GRUB password
   that `biokids-harden` actually set **is** in scope; the unhardened default is not.
3. Removing the drive, or mounting it from another machine.
4. Escaping to a second device — a phone, a tablet, a friend's laptop.
5. A technically capable teenager, or an adult helping the child.
6. Physical access to the parent's own unlocked session.

Also out of scope:

- "The filter did not block site X." That is a blocklist gap. Open a normal issue.
- Anything that requires root, or the parent's PIN, before it begins.
- Vulnerabilities in the Debian packages we depend on — dnsmasq, nftables, Chromium,
  systemd. Report those to the project or to Debian. Do tell us as well if BioKids'
  configuration of them makes the problem worse than it would be by default.
- Denial of service against a machine you already control.
- Missing hardening on a feature the documentation describes as opt-in and off by
  default, when it is off.
- Social engineering of a parent or a child.
- Automated scanner output with no demonstrated impact on this threat model.

If you cannot tell which side of the line something falls on, report it. Triaging an
out-of-scope report costs an hour; missing a real one costs a family.
