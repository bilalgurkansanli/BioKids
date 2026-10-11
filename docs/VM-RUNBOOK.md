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
# Both lines: `git status` checks the working tree, but `git clone` of a local
# path runs git-upload-pack on the .git directory, which is checked on its own.
git config --global --add safe.directory /media/sf_BioKids
git config --global --add safe.directory /media/sf_BioKids/.git

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
V1=$(dpkg-parsechangelog -SVersion)     # 0.1.0-2, the top of debian/changelog
mkdir -p "$D/v1" && cp ../biokids*_${V1}_* "$D/v1/"
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
lintian --fail-on error ../biokids_*.changes
```

**Read the warnings even though they do not fail the build.** On the first run
(lintian 2.122, Debian 13.7) the only one left is expected:

* `initial-upload-closes-no-bugs` on every package. It asks for the Debian ITP
  bug an initial upload closes, and these packages are not uploaded to Debian.

Anything else is new. The first run found two real problems this way — the
postinst did not load debconf, so a `dpkg -i` install never registered the
purge question; and the template text said "answer yes", which means nothing
next to a checkbox — plus four stale overrides and five missing manual pages.
All are fixed; see D-153.

### 1.2 The units, before installing them

```bash
for u in debian/*.service; do echo "== $u"; systemd-analyze verify "$u" || true; done
```

Closes part of `README.source` §7 item 5 — the syntax and directive half. The
runtime half of that item (whether `MemoryDenyWriteExecute=` breaks argon2-cffi,
whether `SystemCallFilter=@system-service` breaks dbus-next) can only be answered
by §2, because it needs the services to actually run.

**Two complaints are expected here and are not defects** — on systemd 257
(Debian 13.7) they are the only ones:

* `Command /usr/bin/biokids-… is not executable` on every unit. Nothing is
  installed yet; §2 is the install.
* `Command 'man biokids-admin(8)' failed with code 16` on the provisioning
  template: its `Documentation=` page is not installed yet either. After §2,
  `man biokids-admin` must open it.

What you are looking for is anything *else*: a misspelled directive, an invalid
value, a `SystemCallFilter` group that does not exist.

### 1.3 The upgrade build — now, while the tools are here

§5.2 tests an upgrade, which needs a second version. Build it here so §5 can run
on a rolled-back machine that has no build tools at all:

```bash
dch --no-auto-nmu -i "Test upgrade" && dpkg-buildpackage -us -uc -b -Ppkg.biokids.nokeyring
V=$(dpkg-parsechangelog -SVersion)      # one revision above $V1: 0.1.0-3
mkdir -p "$D/v2" && cp ../biokids*_${V}_* "$D/v2/"
ls "$D/v1" "$D/v2"                      # the same eight packages, two versions
```

`dch` edits `debian/changelog` in the clone, never in the repository on the host
— one more reason the build does not happen in the shared folder.
`--no-auto-nmu` because otherwise `dch` notices you are not the `Maintainer` and
makes a non-maintainer upload, `0.1.0-2.1`. That upgrades just as well, but the
version is then not the one written above.

An upgrade from `0.1.0-1` is the one that moves the virtual-terminal guards out of
`/etc` (`debian/biokids-shell.maintscript`, `rm_conffile … 0.1.0-2~`). To see that
too, build `v1` from the commit before 0.1.0-2 (`git checkout 435a526` in the clone)
and `v2` from the tip, instead of the `dch` above.

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

**Pass:** every name resolves to a candidate version. On Debian 13.7 all of them
do, `python3-dbus-next` (0.2.3) included.

A name that resolves can still be the wrong package. The first run found
`python3-multipart` in `debian/control`: in trixie that is a different library,
and the panel needs `python3-python-multipart` (D-154). Before trusting a new
name, read its `Homepage:` in `apt-cache show`.

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

**If the postinst fails,** apt stops with the packages unpacked but not
configured (`dpkg -l 'biokids*'` shows `iU` and `iF`) and nothing started. That
is the intended failure. The first run failed exactly this way, on an import that
trixie's argon2-cffi does not have (D-154). Fix the cause, rebuild, and install
the new packages with the same command; dpkg configures them then.

**Watch for:** `journalctl -u biokids-daemon -u biokids-panel -b --no-pager`.
This is the first time either unit has started under its sandbox, and the runtime
half of `README.source` §7 item 5 lives here: `MemoryDenyWriteExecute=yes`
against argon2-cffi and cryptography, and `SystemCallFilter=@system-service`
against dbus-next's epoll and socket use. A daemon that starts and stays up
answers both.

---

## 3. The wizard

Open `http://127.0.0.1:7878` on the VM and work through all nine steps. If the
VM's own desktop is slow, forward the port and use a browser on the host instead
— the panel only ever listens on loopback, and this keeps it that way:

```bash
ssh -N -L 7878:127.0.0.1:7878 -p 2222 <you>@127.0.0.1     # on the Windows host
```

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

**Since D-169 this is one button.** On step 8 press "Telefondan erişimi aç". On a
machine without Tailscale the page says "Telefondan erişim hazırlanıyor…" while
`biokids-remote-setup.service` adds Tailscale's repository (signed by the key
BioKids ships) and installs it with `--no-logs-no-support` already in place; then
it shows Tailscale's sign-in page as a button and a QR code. Sign in with **your**
account, on the VM or by scanning the code with your phone, and the page moves on
to the panel's address. Follow it with:

```bash
journalctl -u biokids-remote-setup -f
ps -o args= -C tailscaled      # must end in --no-logs-no-support
```

**Pass:** no terminal was needed; the client never ran without
`--no-logs-no-support` (`journalctl -u tailscaled -b | grep -i logtail` shows no
upload); step 8 ends on the address and its QR code.

The manual way, which the button replaces and which still works. Tailscale is not
in Debian's archive; it comes from Tailscale's own repository, which has a `trixie`
suite:

```bash
sudo apt install curl
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
  | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
  | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt update && sudo apt install tailscale && sudo tailscale up
```

`tailscale up` prints a link; open it and sign in with **your** Tailscale account.
If the admin console lists the machine but `tailscale status` still says "Logged
out", the approval was lost on the way back (it happened on the first run): run
`sudo tailscale up` again and open the new link.

Before anything else, stop the client sending its own diagnostic logs to
`log.tailscale.com`, which it does by default and which principle #1 does not
allow (D-157):

```bash
sudo sed -i 's/^FLAGS=""$/FLAGS="--no-logs-no-support"/' /etc/default/tailscaled
sudo systemctl restart tailscaled
ps -o args= -C tailscaled      # must end in --no-logs-no-support
```

**A new tailnet needs two switches turned on, once, by its owner**, and until
both are on `tailscale serve` waits without saying so to anyone but a terminal:

* **Serve.** Pressing "Telefondan erişimi aç" now shows the approval page
  Tailscale asked for, as a button (D-157). Before D-157 the panel said only
  "Telefondan erişim ayarı değiştirilemedi".
* **HTTPS certificates**, at <https://login.tailscale.com/admin/dns> → *HTTPS
  Certificates* → *Enable HTTPS*. On the first run approving Serve was not
  enough; `tailscale status --json` showed `"CertDomains": null` until this was
  on, and `serve` kept saying Serve was not enabled.

Tailscale warns, correctly, that enabling HTTPS writes the machine's
`*.ts.net` name into the public Certificate Transparency logs. The name is
public; the panel, its traffic and access to it are not.

Then complete step 8. The wizard now refuses to move on when `tailscale serve`
fails (D-146), so if you land back on step 8 with an error, read
`journalctl -u biokids-panel -b | grep -i remote` before assuming the step is
broken.

**Pass:** step 8 shows a QR code and a `https://<host>.<tailnet>.ts.net` URL, and
opening that URL on your phone **reaches the wizard's language step**.

Not the login screen — setup is not complete until step 9, and while it is
incomplete every route including `/login` redirects to `/wizard/1`. Seeing the
Turkish language step on the phone is the pass. **Do not tap through it on the
phone**: the wizard is unauthenticated by design and a second run from another
device would overwrite the pending child.

### 3.4 The closing screen

Press **Bitir** on step 9. Since D-182 there is no email step; mail is set up
later under **Ayarlar**.

**Pass:** the "Hazır — Kurulum tamam" screen (`/wizard/10`) with the child's
avatar and a "Panele git" button — *not* a bare PIN prompt. That was broken until
recently (D-150) and it is the last thing a parent sees. It asks for **one
restart**: LightDM reads the login-by-picture setting (D-155) only when it starts.
There is no "Adım 10 / 9" above it, and no promise of a trial run.

---

## 4. The child's session

Restart the VM, as the closing screen asked, and choose `elif` at the LightDM
greeter.

**Pass, in order:**

1. Elif is in the greeter's user list, and choosing her shows **no password
   field** — only "Oturum aç" (D-155). A password field means the login-by-picture
   service is not in use: check `sudo lightdm --show-config | grep pam-service`
   and `id elif` (must include `biokids-child`). No Elif at all means §3.1
   failed and you are testing the wrong thing.
2. The session lands directly in the launcher — full screen, tiles, the time
   ring. Not an XFCE desktop.
3. **Zio or Sio is visible** on the first-run card. An empty character slot means
   `/usr/share/biokids/characters/` is missing (D-150).
4. `Alt+F4` does nothing. `Ctrl+Alt+F2` does nothing (properly checked in §6).
5. Tapping a tile opens the app **and the launcher responds immediately** — the
   tile shows it is opening at once, with no loading screen in between, and stops
   when the app's window is in front, never only when you close the app (D-149,
   D-208). A web tile
   (TRT Çocuk, EBA) must open the site: Chromium's "This page is blocked" means
   the daemon's policy does not name it (D-156) — check
   `/etc/chromium/policies/managed/biokids-kiosk.json`. No window may ask the child
   for a keyring password or offer to restore pages.
6. Leave the child's session alone for more than ten minutes — or, from ssh,
   `sudo -u elif DISPLAY=:0 XAUTHORITY=/home/elif/.Xauthority xset s activate`.
   The screen may blank, but a key must bring the launcher back with **no lock
   screen**: an idle lock is one only a parent can lift (D-158).
7. Install `biokids-desktop` **before** the wizard, as §2.1 does: the app step
   offers only what is installed then. Anything installed later is ticked under
   **Çocuklar → Uygulamalar ve siteler** (D-164).

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

**Pass:** Chromium's own "blocked by your administrator" page. Since D-171 the
policy closes `chrome://policy` itself, and in every profile on the machine, so a
page that opens at all means the policy was not read: the kiosk is an ordinary
browser with an ordinary `Ctrl+Shift+I`. Read the file instead:

```bash
sudo python3 -m json.tool /etc/chromium/policies/managed/biokids-kiosk.json | head -40
```

`URLAllowlist`, `URLBlocklist`, `DeveloperToolsAvailability` and
`DownloadRestrictions` must all be there.

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

**Pass, within about five seconds:** the child's screen locks, and the lockdown
says it locked something:

```bash
loginctl list-sessions
journalctl -u biokids-lockdown.service -b --no-pager   # "locked 1 session(s)", not 0
```

"locked 0 session(s)" with a child logged in is the failure this step exists to
catch; it is what every machine did until D-155.

Then, still locked, choose Elif on the unlock screen: **a password field must
appear, and an empty password must be refused.** A child who could lift the lock
by choosing their picture would make the lock decoration. The way back is the
parent's: from the panel, grant Elif ten minutes — the session must come back on
screen without anyone typing anything.

This is the direction that matters. §5.2 checks that lockdown does *not* fire
during an upgrade; this checks that it *does* fire when the daemon dies. Sign
Phase 0 off without it and "fail safe, not silent" has never been observed.

The daemon restarts itself (`Restart=on-failure`), and the child **stays locked**
until you grant time from the panel: a crash is not a reason to let them back in
unsupervised. On the first run this took about ten seconds from the kill to the
greeter, because light-locker shows its own lock window first.

Seen through `VBoxManage controlvm … screenshotpng`, the screen can come back as
horizontal streaks after the session returns from the greeter: that is VirtualBox's
frame buffer after the VT switch, not the session. A mode change redraws it —
`xrandr --output VGA-1 --mode 1024x768` on the child's display.

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

To the `v2` packages built in §1.3. **Upgrade all seven together** — the six and
`biokids-desktop`, which §2.1 installed too. Every package pins `biokids-core`
and `biokids` to `(= ${binary:Version})`, so upgrading a subset leaves the rest
unsatisfiable, and apt resolves that by *removing* what was left out: with only
the six named, the first run's `apt` quietly removed `biokids-desktop`. An
upgrade from the archive brings all of them at once, so this is about the
runbook, not the product:

```bash
sudo apt install "$D"/v2/biokids_*.deb "$D"/v2/biokids-core_*.deb \
  "$D"/v2/biokids-daemon_*.deb "$D"/v2/biokids-panel_*.deb \
  "$D"/v2/biokids-shell_*.deb "$D"/v2/biokids-voice_*.deb \
  "$D"/v2/biokids-desktop_*.deb
```

Read apt's summary before answering: it must say nothing is to be removed.

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

The question comes only after apt has removed the files, and debconf ignores
anything typed before it asks. Over ssh without a person at the keyboard,
`dev/answer_debconf.py` waits for the question and then answers it:

```bash
sudo python3 /media/sf_BioKids/dev/answer_debconf.py hayır \
  env DEBIAN_FRONTEND=readline LANG=tr_TR.UTF-8 apt-get purge -y biokids-core
```

On a Turkish machine the question is in Turkish — "Bu ailenin BioKids verileri
silinsin mi? [evet/hayır]". English means the translation did not reach the
package (D-159). A removal that prints `block: not found` is the other D-159
bug.

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
ls -l /usr/lib/systemd/logind.conf.d/biokids.conf
ls -l /usr/share/X11/xorg.conf.d/50-biokids-no-vt-switch.conf
ls /etc/systemd/logind.conf.d/biokids.conf /etc/X11/xorg.conf.d/50-biokids-no-vt-switch.conf   # gone
sudo systemctl restart systemd-logind   # or reboot; logind drop-ins need it
```

Since 0.1.0-2 the guards are packaged defaults in `/usr/lib` and `/usr/share`, not
conffiles in `/etc`, so `apt remove biokids-shell` takes them away (principle 4)
and an administrator can still override them from `/etc`. On a fresh install the
`/etc` paths never existed; after an upgrade from 0.1.0-1 (§1.3) they must be gone
too, removed by `debian/biokids-shell.maintscript` (a copy someone edited is kept as
`.dpkg-bak`).

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

**Optional, and what a family would do for a less robotic voice (D-161):** the
MBROLA Turkish voices. The voices are in `non-free` and the `mbrola` program in
`contrib`, so add both components to every `deb` line first:

```bash
sudo sed -i 's/ main non-free-firmware/ main contrib non-free non-free-firmware/' /etc/apt/sources.list
sudo apt update && sudo apt install mbrola-tr1 mbrola-tr2
```

Zio then speaks with `tr2` (a woman) and Sio with `tr1` (a man) from the child's
next login; `journalctl --user -u biokids-voice` in the child's session says
`speaking with espeak-ng tuning 2; mbrola: sio=tr1, zio=tr2`. Without them,
espeak-ng speaks with its own female and male variants. Either is a pass.

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

## 8. What 0.1.0-2 added

D-187 to D-194 and D-197, none of which §1–§7 exercises. Run it on the machine from §7, after
the recording, with Elif set up and her quota raised so she is not locked out
halfway. `curl` is used below: `sudo apt install -y curl` if §3.3's manual way did
not already bring it.

### 8.1 The extension is packed and in the policy

```bash
ls -l /var/lib/biokids-ext/                        # biokids.crx, updates.xml, extension-id
sudo ls -l /var/lib/biokids/extension-key.pem      # -rw------- root root
cat /var/lib/biokids-ext/extension-id; echo
grep -A2 ExtensionInstallForcelist /etc/chromium/policies/managed/biokids-kiosk.json
```

**Pass:** the three files exist, the key is 0600 root, and the forcelist holds one
entry, `"<id>;file:///var/lib/biokids-ext/updates.xml"`, whose id is the one in
`extension-id`. No forcelist at all means the postinst could not pack it: run
`sudo biokids-admin pack-extension`, read what it says, then
`sudo systemctl restart biokids-daemon` so the policy is written again; the daemon's
log says "the browser extension is not packed" until then.

### 8.2 A blocked site shows the launcher's page

As Elif, open the **İnternet** tile and type an address the filter closes: the DNS
filter's own canary (`CANARY` in `src/biokids_daemon/dnsfilter.py`), or, with Elif
switched to **Sadece seçtiğim siteler** under **Çocuklar → İnternet**, any site not on
her list, such as `example.com`.

**Pass:** the launcher's page — Zio, "Burası senin için değil" and **Büyüğüme sor** —
not Chromium's "This site can't be reached" or "blocked by your administrator".
Pressing the button puts a site request on Today. If Chromium's own page shows, close
the window and open the tile again: Chromium installs a force-listed extension when
it starts, and a window opened before §8.1 was fixed has none.

### 8.3 The History screen lists the sites

Still as Elif, open two or three sites (her web tiles will do), one of them twice.
Then in the panel, **Geçmiş** → today:

```bash
sudo bkq "select ts_wall, payload_json from usage_event where kind='visit' order by id desc limit 10"
```

**Pass:** each site is one line under **Açılan siteler**, with a first and a last
time. The rows hold `{"host": …}` and nothing else — no path, no query. The site
opened twice within ten minutes has one row (D-187).

### 8.4 Two children at once, two launcher ports

Add a second child, **Ali**, from **Çocuklar → Çocuk ekle**. Log Elif in, then from
ssh bring the greeter up beside her session and log Ali in there:

```bash
sudo XDG_SEAT_PATH=/org/freedesktop/DisplayManager/Seat0 dm-tool switch-to-greeter
```

If Elif's screen is locked when you come back to it, a grant from the panel brings
it back, as in §4.3. With both logged in:

```bash
for u in elif ali; do echo "$u $((7879 + $(id -u $u) % 100))"; done
sudo ss -ltnp | grep -E ':(7879|78[89][0-9]|79[0-7][0-9]) '
sudo nft list chain inet biokids launchers
PORT=$((7879 + $(id -u elif) % 100))
curl -sS -m 3 http://127.0.0.1:$PORT/healthz; echo              # as you: refused
sudo -u elif curl -sS -m 3 http://127.0.0.1:$PORT/healthz; echo # as Elif: JSON
```

**Pass:** two launchers, each listening on the port the first line computed for its
child; two rules per child in the `launchers` chain; the parent's request to Elif's
port is refused, and Elif's own gets `{"biokids":"launcher", …}` (D-188). Ali's port
answers only Ali. If the two ports are the same, the uids are 100 apart: the chain
lists both uids in one rule, the daemon logs the collision, and only one of the two can
be logged in at a time (D-188) — write it down rather than working around it.

### 8.5 Editing a child updates the login screen

**Çocuklar → Düzenle** for Elif: change her picture, and her name to "Elif Nur". Save.

```bash
getent passwd elif | cut -d: -f5        # Elif Nur
ls -l /var/lib/AccountsService/icons/elif
journalctl -u 'biokids-provision@*' -b --no-pager | tail -5
```

**Pass:** "Kaydedildi", a new GECOS name, a new icon file, and at the greeter
(`dm-tool switch-to-greeter` as in §8.4) the new name under the new picture (D-191).
`?error=provision` means the panel could not reach the provisioning unit; §3.1 is
how to tell why.

### 8.6 Removing a child hides it from the login screen

With **Ali logged in**, open **Çocuklar → Düzenle** for Ali, press **"Ali adlı
çocuğu BioKids'ten kaldır"** and give the passphrase.

**Pass, first:** refused — "Çocuğunuz şu anda bilgisayarda…" — and nothing changed.
Log Ali out and try again.

```bash
getent passwd ali | cut -d: -f7          # /usr/sbin/nologin
sudo grep SystemAccount /var/lib/AccountsService/users/ali   # SystemAccount=true
id -nG ali                               # no biokids-child
sudo ls -d /home/ali                     # still there
journalctl -u 'biokids-unprovision@*' -b --no-pager
sudo bkq "select count(*) from child where unix_user='ali'"   # 0
```

**Pass:** Ali is gone from **Çocuklar** and from the greeter, the account and the home
stay, and **Kayıtlar** shows the removal. Adding Ali again with the same account name
must give `/bin/bash` back and put Ali on the greeter again.

### 8.7 A limit per kind of app

Elif's **Süre kuralları** → **Gelişmiş**: set **Oyun** to `0` and save. As Elif, tap a
game (Balonlar or SuperTux).

**Pass:** the launcher's page saying "Bunun bugünlük süresi bitti", with a way back
and **no** "Büyüğüme sor" (D-193). Her learning tiles still open. Empty the field and
save; the game opens again.

### 8.8 Offline Wikipedia only when there is one

Tick **Vikipedi** for Elif under **Çocuklar → Uygulamalar ve siteler**.

```bash
ls /var/lib/biokids-ai/models/wikipedia_*.zim 2>/dev/null || echo "no encyclopedia"
ss -ltn | grep ':7877 '
```

**Pass, without the encyclopedia:** nothing listens on 7877 and Elif's home screen has
no Vikipedi tile (D-190).

Then, if `biokids-ai` can be installed on this machine — it needs `llama.cpp-tools`
newer than Debian 13's, from the BioKids repository — install it with `kiwix-tools`
and download the models under **Çocuklar → Asistan**:

```bash
systemctl status biokids-wiki.path biokids-wiki.service --no-pager
```

**Pass, with it:** `biokids-wiki.service` active, `kiwix-serve` on `127.0.0.1:7877`,
and the Vikipedi tile on Elif's home screen the next time it is drawn, opening the
encyclopedia in her browser. If `biokids-ai` cannot be installed yet, record the first
half as the pass and the second as not run.

### 8.9 The app bar

D-197. biokids-shell has four new dependencies; before installing the build:

```bash
apt-cache policy python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 pulseaudio-utils | grep -B1 Candidate
```

Log Elif in. The helpers below are used from ssh throughout:

```bash
U=elif; R=/run/user/$(id -u $U)
as_elif() { sudo -u $U env XDG_RUNTIME_DIR=$R DBUS_SESSION_BUS_ADDRESS=unix:path=$R/bus \
  DISPLAY=:0 XAUTHORITY=/home/$U/.Xauthority "$@"; }
as_elif systemctl --user status biokids-shell --no-pager | grep 'biokids_shell.appbar'
as_elif xfconf-query -c xfwm4 -p /general/button_layout     # "|": the bar has the button
as_elif xfconf-query -c xfwm4 -p /general/placement_ratio   # 100
journalctl _SYSTEMD_USER_UNIT=biokids-shell.service -b --no-pager | grep -i 'app bar'
```

**Pass, in order** (a screenshot of each is worth keeping:
`as_elif xfce4-screenshooter -f -s /tmp/bar.png`):

1. On the launcher's home screen there is **no** bar.
2. Open **Balonlar** (Frozen Bubble). Its window opens in the middle of the screen,
   its title bar has no button, and at its title bar's top right is the compact bar
   (D-205): 🏠, "⏳ N dk", 🔉, 🔊 and a red ✕ — the emoji in colour, no slider and no
   clock, the whole of it under half the game's width, ✕ at the game's own top
   right corner.
3. With the game's music playing, tap 🔉 twice:

   ```bash
   as_elif pactl list sink-inputs | grep -E 'Sink Input|Volume:|process.id'
   as_elif wpctl get-volume @DEFAULT_AUDIO_SINK@
   ```

   **Pass:** the game's stream went down by 20%, the machine's volume did not move,
   and a time warning (§7) is as loud as before. A game with no sound has both
   volume buttons dimmed. The slider is the whole bar's: over an app that fills the
   screen and plays a sound (a video in a web tile, as in 9), the bar has 🏠,
   "⏳ N dk" with the clock under it, 🔉, a slider, 🔊 and ✕, each button as big as
   the old close button (D-201), and the slider's knob shows the level. Drag the
   knob to the left end, then tap the middle of the slider: the stream goes to 0%,
   then to about 50%, following the finger without lagging behind it.
4. Tap 🏠: the launcher is back and the game keeps running
   (`as_elif systemctl --user list-units --plain --no-legend 'biokids-app-*'`).
   Tap the Balonlar tile: the **same** game comes back, where it was left, and the
   list still has one `biokids-app-frozen-bubble-*` scope, not two.
5. Tap ✕: the game is gone within three seconds (or thirteen, if it had to be
   stopped), its scope is gone from the list, and the launcher is in front.
6. An app that ignores the request. Open Balonlar again and freeze it:

   ```bash
   S=$(as_elif systemctl --user list-units --plain --no-legend 'biokids-app-*' | awk '{print $1; exit}')
   CG=$(as_elif systemctl --user show -p ControlGroup --value "$S")
   sudo kill -STOP $(cat /sys/fs/cgroup$CG/cgroup.procs)
   ```

   Tap ✕. **Pass:** about three seconds later the journal says "did not close when
   asked; stopping its scope" and the window goes. A second tap on ✕ stops it at
   once.
7. The fallback. Kill the bar:
   `sudo pkill -KILL -f '^/usr/bin/python3 -m biokids_shell.appbar$'`.
   **Pass:** `button_layout` is `|C` at once and an app's title bar shows its close
   button again; within about five seconds the bar is back and the layout is `|`.
8. With an app open, let a time warning come (§7.2's quota, or a grant that leaves
   ten minutes). **Pass:** the warning fills the screen with **no** bar on it, and
   the bar is back over the app when the warning hands the screen back.
9. A web tile: a video in YouTube Kids, made full screen. **Pass:** the bar stays
   over it, 🔉 turns the video down, and ✕ closes the browser window. If the volume
   buttons stay dimmed while the video plays, write it down: Chromium's sound
   process did not give its own pid to the sound server.
10. Dragging (D-201). Open a web tile (its window fills the screen, the bar at the
    screen's top right, its strip flush with the title bar and the title bar's bottom
    line running under it). Drag the window by its title bar and keep moving.
    **Pass:** the bar disappears at the first movement — it never trails the window —
    and about a quarter of a second after the finger lets go the window fills the
    screen again with the bar back in the corner. Drag Balonlar the same way:
    **pass** if it stays where it was dropped and the bar is on its own title bar's
    top right, not a title bar's height lower and not left of its corner. If it is
    off, record what the X server and libwnck said:

    ```bash
    sudo apt-get install -y x11-utils    # xprop, xwininfo: the test VM only
    W=$(as_elif xprop -root _NET_ACTIVE_WINDOW | awk '{print $NF}')
    as_elif xprop -id "$W" _NET_FRAME_EXTENTS _GTK_FRAME_EXTENTS WM_CLASS
    as_elif xwininfo -id "$W" | grep -E 'Absolute|Width|Height'
    as_elif systemctl --user set-environment BIOKIDS_LOG_LEVEL=debug
    as_elif systemctl --user restart biokids-shell
    journalctl _SYSTEMD_USER_UNIT=biokids-shell.service -b --no-pager | grep 'from the X server'
    ```

    (`unset-environment BIOKIDS_LOG_LEVEL` and a restart put it back.)
11. Time and clock. **Pass:** the bar's "⏳ N dk" matches the launcher's ring; with
    a grant or quota that leaves under five minutes it turns orange, under one red.
    The clock shows for every child, in the whole bar only.
12. Open apps on the launcher. Open Balonlar, tap 🏠. **Pass:** within a second the
    launcher's footer shows "Açık" with Balonlar's bubble icon and its tile has a teal
    dot; tapping the footer button brings the same game back (one scope, as in 4).
    Close it with ✕: within five seconds of the launcher being in front again, it is
    gone from "Açık" and its tile has no dot.
13. The launcher's own sound bar: drag its knob, tap the bar's ends, and with Tab
    focus it and press ← and →. **Pass:** `as_elif wpctl get-volume
    @DEFAULT_AUDIO_SINK@` follows each.
14. The bar's size (D-205). Switch between a web tile (filling the screen) and
    Balonlar with 🏠 and their tiles, several times. **Pass:** over the web tile
    the whole bar at the screen's top right, over Balonlar the compact one at the
    game's top right, each the moment the app is in front — never the whole bar
    over the game, not even for a moment, and never a bar standing off its corner,
    with a gap after ✕, or with an empty gap between 🏠 and 🔉 where the minutes
    go. Drag Balonlar until half of it is off the screen's right edge: the bar on
    what is left of its title bar - 🏠 and ✕ alone, as what is left is under 520
    pixels - all of it on the screen. A question an app asks in a window narrower
    than that has only 🏠 and ✕ too. For each, the size the bar picked:

    ```bash
    as_elif systemctl --user set-environment BIOKIDS_LOG_LEVEL=debug
    as_elif systemctl --user restart biokids-shell
    journalctl _SYSTEMD_USER_UNIT=biokids-shell.service -b --no-pager | grep 'pixels wide'
    ```

    (`unset-environment BIOKIDS_LOG_LEVEL` and a restart put it back.)
15. A new window fills the screen (D-205). Tick **Yapboz** (Palapeli) for Elif under
    **Çocuklar → Uygulamalar ve siteler** - the app the owner saw open in a window
    little wider than the bar - and open it, three times, closing it with ✕ in
    between. **Pass:** each time, within about two seconds of appearing, it fills
    the screen and stays so, with the whole bar in the screen's top right; it may
    show at its own size for a moment first, but never with the bar on it. Where it
    did, the journal says the bar maximised it, and where the app shrank it after
    it had filled the screen, that the bar saw it do so:

    ```bash
    journalctl _SYSTEMD_USER_UNIT=biokids-shell.service -b --no-pager | grep -E 'does not fill the screen|shrunk by its app'
    ```

    A "does not fill the screen" about a second and a half after the window
    appeared, with no "shrunk by its app" before it, is the bar's late look: the
    window manager did not report the shrinking, and the bar sat on the small
    window until then. Record it.

    The same with **Patates Kafa** (KTuberling) and **Haritalar** (KGeography),
    ticked the same way where they are not on her screen. Balonlar never gets that
    line: a fixed size is left alone. If a KDE app still ends up small, record how
    long after it appeared it shrank (a screen recording, or `xprop -spy` on its
    `_NET_WM_STATE`): the bar looks for six seconds.

### 8.10 Opening an app at once, and never twice

D-208. With the helpers of §8.9, and the launcher's port of §8.4 in `$PORT`:

```bash
as_elif ls -l $R/biokids-appbar.sock      # srw------- elif: the bar listens
scopes() { as_elif systemctl --user list-units --plain --no-legend 'biokids-app-*' | awk '{print $1}'; }
```

**Pass, in order:**

1. Tap **Yapboz** (Palapeli, slow to start). **Pass:** at the tap, the tile has a
   teal ring breathing around it and "Açılıyor…" where its name was; there is no
   "Yapboz açılıyor" screen, and the other tiles and the sound bar still work. The
   ring goes when Palapeli's window comes up, or after 25 seconds.
2. While it is still opening, tap its tile five more times. **Pass:** `scopes` lists
   one `biokids-app-palapeli-*`, not several.
3. Tap 🏠, then the Yapboz tile. **Pass:** the same window is back within a moment,
   `scopes` still lists one, and the journal says "is open already" with no line
   "no app bar to ask … running its helper" (that one is logged at INFO, so its
   absence means the bar answered):

   ```bash
   journalctl _SYSTEMD_USER_UNIT=biokids-shell.service -b --no-pager | grep -E 'open already|no app bar to ask'
   ```
4. Time the answers, as Elif (only her processes reach her launcher):

   ```bash
   as_elif curl -s -o /dev/null -w '%{time_total}\n' -X POST http://127.0.0.1:$PORT/launch/palapeli
   ```

   **Pass:** with Palapeli open, well under half a second (1.46 s before D-208);
   with it closed, under half a second too (1.59 s before), the window following
   in its own time.
5. A busy bar. Freeze it, tap the tile of an app that is open, and thaw it:

   ```bash
   B=$(pgrep -u elif -f -- '-m biokids_shell.appbar$')
   sudo kill -STOP $B; sleep 8; sudo kill -CONT $B    # tap the tile during the sleep
   ```

   **Pass:** no second scope; the journal says the window "could not be looked for;
   not starting it twice", and once the bar is thawed the app's window comes to
   the front. Then the same with `sleep 15`, tapping at its start: once thawed,
   **no** window jumps to the front - the request is stamped with the tap's time
   and is more than ten seconds old - and the journal says "not acted on".
6. No bar. `sudo kill -KILL $B` and, before it is back, tap the tile of an open app.
   **Pass:** the app comes back (the helper, a few seconds), `scopes` lists one, and
   the journal says "no app bar to ask … running its helper".
7. An app that cannot start. Put a failing `tuxpaint` ahead of Debian's on the
   session's PATH:

   ```bash
   printf '#!/bin/sh\necho "tuxpaint: cannot start" >&2\nexit 1\n' | sudo tee /usr/local/bin/tuxpaint >/dev/null
   sudo chmod 755 /usr/local/bin/tuxpaint
   ```

   Tap **Boya**. **Pass:** its tile breathes "Açılıyor…" for about two seconds,
   then the calm Blocked screen; `scopes` lists no `biokids-app-tuxpaint-*`, and the
   journal says "ended as it started, with exit status 1" and "tuxpaint: cannot
   start". Back home, tap it again: the same two seconds, never 25 seconds of the
   ring. Then `sudo rm /usr/local/bin/tuxpaint` and tap it once more: Tux Paint
   opens.

### 8.10 Insights: the week in plain words (D-206)

The upgrade's migration adds one table; the daemon fills it from the tick that
counts the minutes. With Elif logged in and using an app for a few minutes:

```bash
sudo bkq "select version_num from alembic_version"          # 077ca4545f23
sudo bkq "select day, hour, seconds from usage_hour order by day desc, hour desc limit 5"
sudo bkq "select day, general_s from ledger_day order by day desc limit 1"
journalctl -u biokids-daemon -b --no-pager | grep -i 'hours of the day'   # nothing
```

**Pass:** rows for the current hour, growing every thirty seconds, and today's
`usage_hour` seconds add up to today's `general_s` (to within the last half
minute). Then, on the phone, **Analiz** in the bottom bar:

1. **Bu hafta**, **Geçen hafta**, **Son 30 gün** each open, the chosen one filled.
   On a fresh machine the screen says "Henüz yeterli veri yok…" and nothing else.
2. With a day or two of use: up to five sentences under **Öne çıkanlar**, the
   numbers as tiles (two across on the phone, nothing wider than the screen), a
   bar per day with the day's quota as a dashed line, the kinds of app as one bar
   with a legend, the apps, the hours of the day, the requests and the sites.
   A day bar opens that day in **Geçmiş**.
3. A suggestion's link opens the setting it names: "Türlere göre günlük süre"
   lands on **Süre kuralları** with **Gelişmiş** open at the limits.
4. **Bugün** shows a "Bu hafta" card under the children with at most three
   sentences each and "Analizin tamamı". With Elif logged out, `sudo systemctl
   stop biokids-daemon`: the live cards say the service is not answering and the
   week's card is still there. `sudo systemctl start biokids-daemon` afterwards.
5. Sunday after 19:00, with mail set up (D-177), the week's mail ends each child's
   days with "Öne çıkanlar:" and at most three sentences, and names no site.

### 8.11 The apps an upgrade brought (D-207)

On this machine nothing is marked new, and that is right. A tile is new only when
an upgrade's `sync-catalog` adds its slug to a database that already had tiles.
§7's machine was a fresh install, and a machine that already had D-203's apps gets
no new slug from this build:

```bash
sudo biokids-admin sync-catalog      # catalog: 0 added, …
sudo bkq "select value from setting where key='catalog_new_apps'"   # nothing
```

**Pass:** no row, no **Yeni** on any **Uygulamalar ve siteler**, no line under any
child on **Çocuklar**.

To see the feature, make two tiles new by hand. `dpkg -l ktuberling blinken` must
show both installed (a box that cannot be ticked is not named). Take a child who is
five this year (the birth year is under **Düzenle**), untick **Patates Kafa** and
**Hafıza** under **Uygulamalar ve siteler** and save. Only after that save, with the
panel stopped, write the setting at the current UTC time; `bkq` never commits, so
this is a `with con:` block:

```bash
sudo systemctl stop biokids-panel
sudo python3 - <<'EOF'
import datetime as dt, json, sqlite3
now = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(timespec="seconds")
con = sqlite3.connect("/var/lib/biokids/biokids.db")
with con:
    con.execute(
        "insert or replace into setting (key, value) values (?, ?)",
        ("catalog_new_apps", json.dumps({"ktuberling": now, "blinken": now})),
    )
con.close()
EOF
sudo systemctl start biokids-panel
```

The order matters: a save of the child's apps after the apps arrived answers the
line, so unticking them after writing the setting would hide it.

**Pass, in order:**

1. **Çocuklar**: under that child, a link "Bu çocuğun yaşına uygun 2 yeni uygulama
   henüz ekranında yok: Patates Kafa, Hafıza." to the child's apps screen. A child
   of eight or more has no line (Patates Kafa is for 4–6, Hafıza for 5–7).
2. The link: the same line at the top, ending "Eklemek için işaretleyin.", and a
   **Yeni** beside those two tiles and no others, for every child.
3. Save that screen without ticking them. **Pass:** the line is gone from both
   screens and the two **Yeni** stay. They go by themselves thirty days after the
   time written above.

---

## 9. After the run

Only once §1–§8 have all passed:

1. `docs/ROADMAP.md` — mark v1.0's exit criterion met.
2. `README.md` — add the recording from §7.5. The three panel screenshots in
   `assets/screenshots/` are current; `dev/screenshots.py` also defines
   `shell-home`, `shell-blocked` and `shell-first-run`, taken against a launcher
   given with `--shell`. Since D-188 that is the child's own port (§8.4), and only
   the child's own processes may reach it. Those have never been captured.
3. Tag the release: `release.yml` runs on a `v*` tag and publishes the APT
   repository, once the archive key exists (`docs/ROADMAP.md`). The tag must match
   the upstream version at the top of `debian/changelog` (`docs/PACKAGING.md` §1).
4. Open the pinned Roadmap issue.

---

## 10. What to do with what you find

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
