# VM runbook — T8.5 and T9

Everything standing between this repository and Phase 0 being done, in the order
it has to happen, with the command to run and what a pass looks like.

**Why this file exists.** Phase 0 was written on Windows. `dpkg-buildpackage`,
`lintian`, `piuparts` and `systemd-analyze` have never run against it, and no
part of the product has ever started under systemd. The code is tested — 541
tests, `mypy --strict`, two adversarial review passes — and none of that tells
you whether the `.deb` installs. This is the part only a VM can answer.

Read `debian/README.source` §7 alongside this. It lists what could not be
verified from Windows, and every one of its ten items is closed by a numbered
step below; where a step exists only to close one, it says so.

**Every command here has been checked against the repository** — paths, unit
names, flags, and each "pass" condition read against the code that produces it.
That is not the same as having been run. Where a step is expected to fail as
written on today's tree, it says so rather than leaving you to discover it.

---

## 0. Before you start

Everything in this section happens **once, before the snapshot**, so that every
rollback returns to a machine that already has it.

### 0.0 On a Windows 11 host: VirtualBox must get VT-x

Found the hard way on the first real attempt, and it cost two installs. If
Windows keeps its own hypervisor running, VirtualBox cannot use VT-x and falls
back to running through Hyper-V ("NEM"). Linux guests are slow there and can
hang outright — the Debian installer froze mid-download with the keyboard's
Caps Lock and Scroll Lock blinking, which is a guest kernel panic.

**Check, after starting any VM once:**

```powershell
Select-String "$env:USERPROFILE\VirtualBox VMs\<vm>\Logs\VBox.log" -Pattern "fall back to NEM|VT-x w/"
```

`VT-x w/ nested paging` is good. `Attempting fall back to NEM` means stop here.

On Windows 11 **three** separate features keep the hypervisor on, and each has to
be off; turning off only the first changes nothing you can see:

| Feature | How to turn it off |
|---|---|
| Memory Integrity (HVCI) | Windows Security → Device security → Core isolation → Memory integrity: Off |
| Virtualization-Based Security | `reg add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard" /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 0 /f` |
| Windows Hello Enhanced Sign-in Security | `reg add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\WindowsHello" /v Enabled /t REG_DWORD /d 0 /f` |

plus `bcdedit /set hypervisorlaunchtype off`, all from an administrator
terminal, then a reboot. To see what is still holding it:

```powershell
(Get-CimInstance Win32_ComputerSystem).HypervisorPresent
Get-ChildItem HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios | % { "$($_.PSChildName) $((Get-ItemProperty $_.PSPath).Enabled)" }
```

**These are Windows security features, and turning them off has costs** the
owner of the machine should decide on. The one that surprised us: **turning off
Enhanced Sign-in Security invalidates the Windows Hello PIN**, because the PIN's
keys were held inside VBS — Windows asks for a new PIN at the next sign-in, and
face or fingerprint may need re-enrolling. All of it is reversible: `/d 1`,
`hypervisorlaunchtype auto`, Memory Integrity back on — at which point VirtualBox
drops back to NEM.

If a machine will not give up its hypervisor, **use Hyper-V instead of
VirtualBox** rather than peeling off further security layers. This runbook is
written for VirtualBox and would need adapting, but that is cheaper than a host
you no longer trust.

### 0.1 The VM

Debian 13 (trixie) netinst in VirtualBox: 4 GB RAM, 2 vCPU, 30 GB disk (T0.1).
The ISO is `debian-13.x.y-amd64-netinst.iso` from
<https://www.debian.org/CD/netinst/> — the `amd64` link.

Five settings that a first install gets wrong, in the order you meet them:

1. **Skip VirtualBox's unattended install.** VirtualBox 7 selects it by default
   when you attach an ISO. The checkbox is worded differently between versions —
   *"Skip Unattended Installation"*, which must be **ticked**, or *"Proceed with
   Unattended Installation"* (Turkish UI: *"Katılımsız Kurulumla Devam Et"*),
   which must be **unticked**. Either way the goal is the same: no unattended
   install. It chooses its own desktop and user and never shows the
   Software selection screen, so neither Xfce nor the SSH server gets picked.
   **How to tell it happened anyway:** the installer never asks you a question,
   and the finished machine logs in as `vboxuser` to a GNOME desktop. That
   machine cannot be used here — GNOME runs on Wayland and BioKids is X11 and
   Xfce only (D-12) — so delete it and start again. This is easy to miss; it
   happened on the first real attempt.
2. **SSH port forward**, in the VM's network settings before first boot: NAT →
   Port Forwarding → host `127.0.0.1:2222` to guest port `22`. Under NAT the
   guest's own address (`10.0.2.15`) is not reachable from the host, so the
   forward is what makes `ssh -p 2222 <you>@127.0.0.1` work — and unlike a
   bridged adapter, it works on any network the host happens to be on.
3. **Leave the root password empty** in the Debian installer. Setting one means
   the first user is *not* in the `sudo` group, and every `sudo` below fails.
4. At **Software selection**, **untick GNOME** — it is ticked by default — and tick
   **Xfce**, **SSH server** and **standard system utilities**. With GNOME left
   ticked both desktops install and GNOME's session may become the default.
5. **Graphics controller: VBoxSVGA**, not the default VMSVGA (Settings → Display,
   3D acceleration off, 128 MB). Found on the first real install: Debian 13's
   `vmwgfx` driver logs *"vmwgfx seems to be running on an unsupported
   hypervisor ... Please switch to a supported graphics device"* and the machine
   stops at a black console after `/dev/sda1: clean`, with no login screen.
   VirtualBox may flag VBoxSVGA with a yellow "invalid settings" note, because
   it recommends VMSVGA for Linux; ignore it.

SSH is chosen there on purpose. §6 disables Ctrl+Alt+F1–F12, and a shell that
does not go through the graphical session is how you get back into a broken one.
Put in by the installer it is part of the snapshot and survives every rollback;
installed afterwards, each rollback would quietly take it away.

Then the Guest Additions (Devices → Insert Guest Additions CD image). If the
installer asks for a compiler and kernel headers:
`sudo apt install build-essential linux-headers-amd64`.

Then a shared folder: **name `BioKids`**, pointing at the repository on the host
(`C:\Users\bilal\Desktop\BioKids`), **auto-mount** on. It appears at
`/media/sf_BioKids`, and reading it needs the `vboxsf` group:

```bash
sudo adduser "$USER" vboxsf     # then log out and back in
ls /media/sf_BioKids/debian     # must list files
```

And from the Windows host, that the port forward works:

```bash
ssh -p 2222 <you>@127.0.0.1 true && echo ssh-ok
```

### 0.2 Two accounts

Create a parent account (`anne`, or your own name) and **do not create a child
account by hand.** Creating the child is the product's job — §3.1 — and doing it
yourself hides the single riskiest unknown in this runbook.

### 0.3 Three things every later step assumes

```bash
sudo apt install -y git

# Where built packages live: on the host, where no rollback can reach them.
# `dist/` is already in .gitignore.
echo 'export D=/media/sf_BioKids/dist' >> ~/.bashrc && . ~/.bashrc

# git refuses a repository owned by another user, and vboxsf files are root's.
git config --global --add safe.directory /media/sf_BioKids

# A read-only way into the database.
sudo tee /usr/local/bin/bkq >/dev/null <<'EOF'
#!/bin/sh
exec python3 -c "
import sqlite3, sys
con = sqlite3.connect('/var/lib/biokids/biokids.db')
for row in con.execute(sys.argv[1]):
    print(*row)
" "$1"
EOF
sudo chmod 755 /usr/local/bin/bkq
```

`bkq` exists because the `sqlite3` command-line tool is not on a stock Debian 13
machine and no BioKids package pulls it in — `biokids-core` depends on
`python3-sqlalchemy`, which is the Python binding, not the tool. It lives in
`/usr/local/bin` rather than `/tmp` because `/tmp` is emptied at every boot and
§7.3 reboots. Use it as `sudo bkq "select name from sqlite_master where type='table'"`.

### 0.4 Snapshot

Now, and not before: **snapshot as `clean-debian-13`.** You roll back to it three
times — before §5, §6 and §7. §1 to §4 run on this machine, in order.

The snapshot holds SSH, `git`, `bkq` and `$D`, and **no Python packaging tools**.
That is deliberate. `dh-python`, `python3-setuptools` and friends on a test
machine could quietly satisfy a runtime dependency `debian/control` forgot to
declare, and the clean installs in §5 and §7 exist to catch exactly that. So
every package the later sections need is built in §1, while the tools are here.

---

## 1. Build the packages

**On the VM's own disk, not in the shared folder.** `dpkg-buildpackage` writes
the `.deb` files into the *parent* directory. From `/media/sf_BioKids` that is
`/media/`, which is not shared and would be gone at the next rollback — and a
shared folder is a fragile place to run a build in any case. So clone, build, and
copy the results onto the host:

```bash
git clone /media/sf_BioKids ~/src/BioKids && cd ~/src/BioKids
sudo apt-get install -y --no-install-recommends build-essential devscripts debhelper dh-python gnupg po-debconf pybuild-plugin-pyproject python3-all python3-setuptools lintian
chmod +x debian/rules && dpkg-buildpackage -us -uc -b -Ppkg.biokids.nokeyring
mkdir -p "$D/v1" && cp ../biokids*_0.1.0-1_* "$D/v1/"
```

The clone is of what is **committed**; edits on the host that are not committed
are not in it. `debian/rules` is committed executable, so on a clone the `chmod`
changes nothing — it is there for a checkout that lost the bit.

`-Ppkg.biokids.nokeyring` skips `biokids-archive-keyring`, which needs the real
archive key and can only be built by the release workflow.

**Pass:** eight `.deb` files in `$D/v1`, and no error.

**If `dh_install` fails on a glob that matches nothing**, run
`python3 dev/check_packaging.py` — it checks that exact class of mistake from the
source tree and should have caught it before you got here. If the checker passes
and the build still fails, the checker has a gap; fix both.

### 1.1 lintian

```bash
lintian --fail-on error --suppress-tags bad-distribution-in-changes-file ../biokids_*.changes
```

`bad-distribution-in-changes-file` is suppressed because the changelog targets
trixie, which lintian does not recognise as a distribution name until it is
released. **Read the warnings even though they do not fail the build** — this is
the first time any of them has been seen.

### 1.2 The units, before installing them

```bash
for u in debian/*.service; do echo "== $u"; systemd-analyze verify "$u" || true; done
```

Closes part of `README.source` §7 item 5 — the syntax and directive half. The
runtime half of that item (whether `MemoryDenyWriteExecute=` breaks argon2-cffi,
whether `SystemCallFilter=@system-service` breaks dbus-next) can only be answered
by §2, because it needs the services to actually run.

**Four kinds of complaint are expected here and are not defects:**

* `Command /usr/bin/biokids-… is not executable` on every unit. Nothing is
  installed yet; §2 is the install.
* `Unit biokids-lockdown.service not found` from the daemon's `OnFailure=`. The
  file is `debian/biokids-daemon.biokids-lockdown.service` and only
  `dh_installsystemd --name=` renames it on the way into the package.
* `Unit biokids-voice.service not found` from the shell's `Wants=`. Same reason:
  the file is `debian/biokids-voice.user.service`.
* Complaints about `graphical-session.target` and `pipewire.service` on the two
  `*.user.service` files. Those are **user** units and this loop verifies them as
  system units.

What you are looking for is anything *else*: a misspelled directive, an invalid
value, a `SystemCallFilter` group that does not exist.

### 1.3 The upgrade build — now, while the tools are here

§5.2 tests an upgrade, which needs a second version. Build it here so §5 can run
on a rolled-back machine that has no build tools at all:

```bash
dch -i "Test upgrade" && dpkg-buildpackage -us -uc -b -Ppkg.biokids.nokeyring
V=$(dpkg-parsechangelog -SVersion)      # 0.1.0-2
mkdir -p "$D/v2" && cp ../biokids*_${V}_* "$D/v2/"
ls "$D/v1" "$D/v2"                      # the same eight packages, two versions
```

`dch` edits `debian/changelog` in the clone, never in the repository on the host
— one more reason the build does not happen in the shared folder.

---

## 2. Install

### 2.0 The dependency names — `README.source` §7 item 3

Every Debian package name in `debian/control` was written from memory on
Windows. This is the first command that resolves them against a real archive, and
a wrong one does not look like a BioKids bug — it looks like apt refusing the
whole set with "unmet dependencies".

```bash
apt-cache policy python3-argon2 python3-cryptography python3-dbus-next \
  python3-fastapi python3-uvicorn python3-jinja2 python3-multipart \
  python3-itsdangerous python3-qrcode alembic python3-sqlalchemy \
  dnsmasq nftables espeak-ng chromium lightdm xfce4 2>&1 | grep -B1 -i "unable\|not found\|^$" | head -40
```

**Pass:** every name resolves to a candidate version. `python3-dbus-next` is the
one `README.source` singles out as most likely to be wrong; if it is not in
trixie, that is a T3.1 decision and not something to paper over here.

### 2.1 Install

```bash
sudo apt install "$D"/v1/biokids_*.deb "$D"/v1/biokids-core_*.deb \
  "$D"/v1/biokids-daemon_*.deb "$D"/v1/biokids-panel_*.deb \
  "$D"/v1/biokids-shell_*.deb "$D"/v1/biokids-voice_*.deb
```

Then the desktop metapackage, which pulls XFCE, LightDM, Chromium and the
application catalogue:

```bash
sudo apt install "$D"/v1/biokids-desktop_*.deb
```

**Pass:**

```bash
systemctl is-active biokids-daemon biokids-panel   # active, active
sudo ls -l /var/lib/biokids/biokids.db             # exists, 0600, root
getent group biokids                                # exists
sudo alembic -c /usr/share/biokids/alembic.ini current   # a head revision
```

The last one is the postinst's `alembic upgrade head`. No revision means the
migration did not run, and a daemon started against a schema-less database
crash-loops — which pulls in `biokids-lockdown.service`.

**Watch for:** `journalctl -u biokids-daemon -u biokids-panel -b --no-pager`.
This is the first time either unit has started under its sandbox, and the runtime
half of `README.source` §7 item 5 lives here: `MemoryDenyWriteExecute=yes`
against argon2-cffi and cryptography, and `SystemCallFilter=@system-service`
against dbus-next's epoll and socket use. A daemon that starts and stays up
answers both.

---

## 3. The wizard

Open `http://127.0.0.1:7878` on the VM and work through all ten steps.

### 3.1 Can the sandboxed panel reach systemd? — `README.source` §7 item 1

The highest-risk unknown in the whole runbook, so do it first.

At step 4 the panel calls `systemctl start --wait biokids-provision@<child>.service`
because its own sandbox (`ProtectSystem=strict`, `ProtectHome=yes`) makes `/etc`
read-only and `/home` an empty tmpfs (D-142). Connecting to the system bus from
such a unit is *expected* to work — a read-only mount does not stop `connect(2)`
— but it has never been tried.

Add a child called `Elif`, account name left blank.

```bash
getent passwd elif
sudo ls -la /home/elif/.config/autostart/           # biokids-shell.desktop
sudo ls -la /home/elif/.config/xfce4/xfconf/xfce-perchannel-xml/
sudo stat -c '%U %G %a' /home/elif/.config          # elif elif — not root
journalctl -u 'biokids-provision@*' -b --no-pager
```

**Pass:** the account exists, the files are there, and `~/.config` belongs to
`elif`. A root-owned `~/.config` is a child who cannot start a session at all.

**If it fails:** the wizard redirects to `/wizard/5?error=provision` and the page
carries the message — the failure is visible and deliberately **not** blocking,
because stopping the setup there would strand a parent halfway through on a
machine where `adduser` is simply absent. Repair by hand:

```bash
sudo biokids-admin provision-child elif --name Elif
```

If that works but the panel's call did not, **the sandbox is the problem** and
the fix belongs in `debian/biokids-panel.service`, not in `provision.py`.

### 3.2 A hyphenated child — item 1, sub-item (c)

`provision_via_systemd` escapes `-` as `\x2d` before interpolating the name into
a unit name. That code has never run. `elif` has no hyphen, so add a second child
from **Çocuklar → Çocuk ekle** with the account name typed as `elif-nur`:

```bash
systemd-escape 'elif-nur'                          # must print elif\x2dnur
getent passwd elif-nur
journalctl -u 'biokids-provision@*' -b --no-pager  # which instance ran
```

**Pass:** the escape matches and the account exists. A wrong escape means a
second child silently gets no account.

### 3.3 Step 8: Tailscale

```bash
sudo apt install tailscale && sudo tailscale up
```

Then complete step 8. The wizard now refuses to move on when `tailscale serve`
fails (D-146), so if you land back on step 8 with an error, read
`journalctl -u biokids-panel -b | grep -i remote` before assuming the step is
broken.

**Pass:** step 8 shows a QR code and a `https://<host>.<tailnet>.ts.net` URL, and
opening that URL on your phone **reaches the wizard's language step**.

Not the login screen — setup is not complete until step 10, and while it is
incomplete every route including `/login` redirects to `/wizard/1`. Seeing the
Turkish language step on the phone is the pass. **Do not tap through it on the
phone**: the wizard is unauthenticated by design and a second run from another
device would overwrite the pending child.

### 3.4 Step 11

Press **Bitir** on step 10.

**Pass:** the "Hazır — Kurulum tamam" screen with the child's avatar and a
"Panele git" button — *not* a bare PIN prompt. That was broken until recently
(D-150) and it is the last thing a parent sees.

---

## 4. The child's session

Log out and log in as `elif` from the LightDM greeter.

**Pass, in order:**

1. There is an avatar for Elif on the greeter at all. (No avatar means §3.1
   failed and you are testing the wrong thing.)
2. The session lands directly in the launcher — full screen, tiles, the time
   ring. Not an XFCE desktop.
3. **Zio or Sio is visible** on the first-run card. An empty character slot means
   `/usr/share/biokids/characters/` is missing (D-150).
4. `Alt+F4` does nothing. `Ctrl+Alt+F2` does nothing (properly checked in §6).
5. Tapping a tile opens the app **and the launcher responds immediately** — the
   loading screen must not stay up until you close the app (D-149).

### 4.1 The user units — `README.source` §7 item 8

The user units are `WantedBy=graphical-session.target`, which assumes XFCE's
systemd session integration is active. If it is not, their `[Install]` sections
are decoration and nothing starts the launcher.

From an ssh session, while the child is logged in:

```bash
sudo -u elif XDG_RUNTIME_DIR=/run/user/$(id -u elif) systemctl --user list-units 'graphical-session*'
sudo -u elif XDG_RUNTIME_DIR=/run/user/$(id -u elif) systemctl --user status biokids-shell.service biokids-voice.service
```

**Pass:** `graphical-session.target` is listed and active, and both user units
are active. If the target is missing, the session configuration in
`src/biokids_shell/xfce/` has to start them explicitly and that is a T5.7 fix.

### 4.2 The kiosk policy

In the child's Chromium (a web tile), open `chrome://policy`.

**Pass:** `URLAllowlist`, `URLBlocklist`, `DeveloperToolsAvailability` and
`DownloadRestrictions` are all listed, with `Source: Platform`. An **empty**
policy page means the kiosk is an ordinary browser with an ordinary
`Ctrl+Shift+I`.

```bash
sudo ls -l /etc/chromium/policies/managed/biokids-kiosk.json
journalctl -u biokids-daemon -b | grep -i "managed policy"
```

The **daemon** writes this file at startup — it is the only BioKids process that
can, because the directory is root-owned and its unit is the one whose
`ReadWritePaths` names it. If the file is missing, the daemon's log says why at
error level.

If Chromium on trixie reads a different directory (some derivatives use
`/etc/chromium-browser/`), that is a one-line fix to `chromium_policy_dir` in
`/etc/biokids/config.toml`, not a code change.

### 4.3 T3.7 — kill the daemon, the child gets locked

`README.source` §7 item 7, and the whole of principle #3. Nothing has ever run
this path on a machine. With the child still logged in:

```bash
sudo systemctl kill -s KILL biokids-daemon
```

**Pass, within about five seconds:** the child's screen locks.

```bash
loginctl list-sessions
journalctl -u biokids-lockdown.service -b --no-pager
```

This is the direction that matters. §5.2 checks that lockdown does *not* fire
during an upgrade; this checks that it *does* fire when the daemon dies. Sign
Phase 0 off without it and "fail safe, not silent" has never been observed.

Restart the daemon afterwards: `sudo systemctl start biokids-daemon`.

---

## 5. T8.5 — install, upgrade, remove, purge

Roll back to `clean-debian-13` first. This sequence is about dpkg state, and a
machine you have been poking at is not a clean subject. Re-install the packages
as in §2 and run the wizard.

### 5.1 Use it

Let the child use the machine for a few minutes so there is real data.

```bash
sudo bkq "select count(*) from ledger_day"
```

Note the number. It has to survive the next two steps.

### 5.2 Upgrade

To the `v2` packages built in §1.3. **Upgrade all six together:** every package
pins `biokids-core` and `biokids` to `(= ${binary:Version})`, so upgrading a
subset leaves the rest unsatisfiable:

```bash
sudo apt install "$D"/v2/biokids_*.deb "$D"/v2/biokids-core_*.deb \
  "$D"/v2/biokids-daemon_*.deb "$D"/v2/biokids-panel_*.deb \
  "$D"/v2/biokids-shell_*.deb "$D"/v2/biokids-voice_*.deb
```

**Do the upgrade while the child is logged in with the launcher open.** That is
the case D-140 was about: the launcher holds a subscription open, the daemon
could not stop, systemd SIGKILLed it after 90 s, `$SERVICE_RESULT` became
`timeout`, and the unit's own `ExecStopPost` fired a lockdown — so an `apt
upgrade` locked a child out of a machine they were using.

**Pass:**

* the units come back up (`systemctl is-active biokids-daemon biokids-panel`);
* the ledger row count is unchanged;
* **`/etc/biokids/config.toml` still has your edits.** Edit it before upgrading —
  change `tick_seconds` — and confirm dpkg either keeps it or asks. A conffile
  prompt is correct; silently overwriting is not;
* the child was not locked out:

```bash
journalctl -u biokids-daemon -b | grep -i -E 'timeout|lockdown|killed'
loginctl list-sessions
```

### 5.3 Remove

```bash
sudo apt remove biokids-core
sudo ls -l /var/lib/biokids/biokids.db   # MUST still exist
```

**Pass:** the data survives. `remove` is not `purge`, and a family that removes
the package to troubleshoot must not lose their child's history.

### 5.4 Purge — asks first, and the answer is obeyed

```bash
sudo apt purge biokids-core
```

**Pass:** a debconf prompt appears — "Delete the stored BioKids data for this
family?", defaulting to **no**. Answer **no**:

```bash
sudo ls -l /var/lib/biokids/   # data still there
```

**Reinstall before purging again.** The first purge completes fully, so a second
`apt purge biokids-core` prints "not installed, so not removed" and never runs
`postrm` — the "yes" branch would never be exercised. Reinstalling also
re-registers the debconf template and, through `debian/biokids-core.config`,
clears the `seen` flag so the question is asked again:

```bash
sudo apt install "$D"/v1/biokids-core_*.deb "$D"/v1/biokids-daemon_*.deb "$D"/v1/biokids-panel_*.deb
sudo apt purge biokids-core          # answer YES this time
sudo ls -l /var/lib/biokids/         # gone
```

Then reinstall once more and confirm a fresh install finds no family and starts
the wizard.

### 5.5 Purge — nobody to ask

Reinstall, then:

```bash
sudo DEBIAN_FRONTEND=noninteractive apt purge biokids-core
sudo ls -l /var/lib/biokids/   # data MUST still exist
```

**Pass:** the data survives. An unattended purge cannot ask, and the answer to a
question nobody heard is no.

---

## 6. T5.7 — the virtual terminal guards

Roll back to `clean-debian-13`, and **snapshot again before installing** — this
is the step that can leave you with a graphical session you cannot get back into.
SSH came with the snapshot (§0.1); confirm it answers **before** installing
anything, because the packages are what disable the VT switch:

```bash
ssh -p 2222 <you>@127.0.0.1 true && echo ssh-ok     # from the Windows host
```

Then install as in §2, **run the wizard and add Elif** — there is no child
account on a rolled-back machine, and §0.2 forbids making one by hand.

```bash
sudo ls -l /etc/systemd/logind.conf.d/biokids.conf
sudo ls -l /etc/X11/xorg.conf.d/50-biokids-no-vt-switch.conf
sudo systemctl restart systemd-logind   # or reboot; logind drop-ins need it
```

Log in as the child and try, in the child's session:

* `Ctrl+Alt+F2` … `Ctrl+Alt+F6` — **pass:** nothing happens, or a blank VT with
  no login prompt.
* `Ctrl+Alt+Backspace` — **pass:** the X server does not die.

```bash
systemctl status getty@tty2   # inactive; NAutoVTs=1 means no getty past VT1
```

**Recovery, if the machine will not start a graphical session:** GRUB's recovery
entry, or ssh. `ReserveVT=1` is deliberate so one VT stays allocated; both config
files say this in their own comments. If you cannot recover, roll back and treat
it as a finding.

---

## 7. T9 — the exit criterion, end to end

Roll back to `clean-debian-13`. This is the run you record.

The criterion, verbatim from `docs/TASKS-PHASE0.md`:

> on a fresh Debian 13 VM, `apt install biokids` → wizard → the parent reaches
> the panel from a phone over Tailscale → sets a 20-minute quota → the child
> session counts down, speaks warnings at 10/5/1 minutes in Turkish, and the
> machine shuts down. Reboot resumes correctly. Moving the system clock forward
> three hours changes nothing.

### 7.1 Voice first

The warnings must be **spoken**, and espeak-ng is what speaks them (D-137):

```bash
sudo apt install espeak-ng
espeak-ng -v tr "Beş dakika kaldı."     # you must hear this
```

If you hear nothing, fix the VM's audio before starting the run — VirtualBox
audio is off by default in some configurations.

`mbrola-tr1`/`tr2` are a `Suggests` and would improve the Turkish, but
**installing them changes nothing today**: `EspeakSynth` always builds
`-v tr`, and an mbrola voice is only reached as `-v mb-tr1`. Using them needs a
code change, so leave them out of this run.

### 7.2 The run

1. Install, complete the wizard, add Elif.
2. On your phone, over Tailscale, open the panel and set a **20-minute** weekday
   and weekend quota. Do this from the phone — it is part of the criterion.
3. Log in as Elif. Start an app.
4. Watch, from the phone, that the Today card counts down and the ring shrinks.
5. At 10, 5 and 1 minute: a spoken Turkish warning **and** a character card.
6. At zero: the lock screen with a 120-second countdown, then the machine powers
   off.

**Do not shorten the wait with `dev/fakeclock.py` for the recording.** That
script drives an injected clock in-process; it proves the ledger's arithmetic and
cannot tell you whether the real daemon speaks through real PipeWire. Use it
beforehand (`python3 dev/fakeclock.py`) to check the timeline is what you expect,
then do the real twenty minutes once.

### 7.3 Reboot resumes

Set the quota to 20 minutes, use 12, then reboot. Log back in as Elif.

**Pass:** roughly 8 minutes remain — not 20. What makes this work is that usage
is persisted per calendar day in `ledger_day` and read back on start; the boot
epoch is what stops a *reboot* being counted as elapsed time, which is the
neighbouring guarantee.

### 7.4 The clock

**Run this before about 20:00.** The quota day is the wall-clock date
(`day = reading.wall.date()`), each date gets its own `ledger_day` row, and
`_carry_over` deliberately moves only an unfinished penalty or break — a new day
is a new allowance. So a +3 hour jump that crosses midnight *correctly* gives the
child a fresh quota, and you would be reading a pass as a failure.

```bash
sudo timedatectl set-ntp false
sudo timedatectl set-time "$(date -d '+3 hours' '+%Y-%m-%d %H:%M:%S')"
```

**Pass:** the remaining time does not change. An audit row appears ("Sistem saati
değişti"), and the ledger is untouched — time is counted on `CLOCK_BOOTTIME`, not
on the wall clock. Put the clock back afterwards:

```bash
sudo timedatectl set-ntp true
```

### 7.5 The recording

Record §7.2 as a short screen capture — phone and screen together if you can.
`TASKS-PHASE0.md` T9 calls this "the first public artifact of the project".

Keep it short and unedited. The thing worth showing is the countdown, the spoken
warning, and the machine going down — not the wizard.

---

## 8. After the run

Only once §1–§7 have all passed:

1. `docs/ROADMAP.md` — mark Phase 0 complete.
2. `README.md` — add the recording from §7.5. The three panel screenshots in
   `assets/screenshots/` are current; `dev/screenshots.py` also defines
   `shell-home`, `shell-blocked` and `shell-first-run` against the launcher on
   `:7879`, and those have never been captured. This VM is the first machine
   where they can be.
3. Tag `v0.1.0`.
4. Open the pinned Roadmap issue.

**Before any of that, the repository needs a remote.** As of this runbook nothing
has been pushed anywhere, and the only branch is `master`:

```bash
git log --oneline | wc -l     # read what you are about to publish
git branch -M main            # CLAUDE.md §6 says PRs go into `main`
git remote add origin git@github.com:bilalgurkansanli/BioKids.git
git push -u origin main
```

---

## 9. What to do with what you find

Every step above exists because something could not be checked from Windows. When
one fails:

* **Write the finding down before fixing it.** `docs/DECISIONS.md` is where the
  reasoning lives, and "we found this on the VM" is the most valuable kind of
  entry in it.
* **Fix the doc in the same commit as the code** (`TASKS-PHASE0.md`,
  `README.source` §7). A spec that drifts from the code is worse than no spec.
* **If a test could have caught it, add the test** — even when the test has to
  assert something indirect, like a path in a `.service` file. Several of the
  review's findings were only reachable because nothing read `debian/`.

Between attempts at §3 and §7, this throws away the data without reinstalling:

```bash
sudo sh /media/sf_BioKids/dev/reset.sh --system
```

From the shared folder, so it works on a rolled-back machine with no clone; and
through `sh` explicitly, because the file is committed without the execute bit. It does not touch `/etc/biokids`: the
configuration and the secret key belong to the machine rather than to the family,
and losing the key loses every stored SMTP password with it.
