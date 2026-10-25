# The BioKids image

Debian 13 "Trixie" with Xfce, BioKids, the child's apps and the assistant's runtime,
as one ISO for a USB stick (D-249). It boots as a live system for trying, and
installs itself with Debian's own installer, which copies that system to the disk:
an install needs no network. On a computer with Windows the installer offers to
shrink it and install beside it; the boot menu then lists both.

## Build

On Debian 13 (the development VM's build image), from a clean `git` checkout - not
a Windows working tree, whose line endings and file modes the image would carry:

    sudo apt install live-build
    dpkg-buildpackage -us -uc -b -Ppkg.biokids.nokeyring   # the .debs, in ..
    cd iso
    sudo make DEBS=../..                                      # 30-60 minutes

The result is `biokids-1.5.0-amd64.hybrid.iso` and its `SHA256SUMS`. `sudo make
clean` before building again with changed configuration: live-build caches.

## What is in it

- `config/package-lists/biokids.list.chroot`: the Xfce desktop as a normal Debian
  install has it, `biokids-desktop` and its apps, `biokids-ai` with `llama.cpp`,
  `whisper.cpp` and `kiwix-tools`. The BioKids .debs and the assistant's runtime
  (rebuilt for Trixie, `dist/ai`) are copied into `config/packages.chroot` by `make`.
  No model is in the image: a parent downloads them from the panel.
- `config/hooks/normal/9000-biokids-image.hook.chroot`: removes Firefox (D-237) and
  what must differ between computers - the key that encrypts the mail password, the
  extension's signing key, the machine id - and checks the database holds no family.
- `config/includes.chroot`: `biokids-firstboot.service`, which makes each installed
  computer's own keys before the daemon starts; GRUB told to look for Windows
  (`os-prober`); a live-config script that keeps the console reachable on the stick.
- `config/includes.installer/preseed.cfg`: only what a family would never want to be
  asked (no root password, no package survey, the time zone); the language, the
  keyboard, the parent's account and the disk stay the installer's questions.
- `auto/config`: the live session masks BioKids' services on its kernel command
  line - the stick is for trying and installing; the installed computer is the one
  that is supervised.

## Test order

VirtualBox with BIOS, then with EFI, then a real computer with Windows on it. The
first two passing say little about the third: wifi firmware, Secure Boot (Debian's
signed shim, D-17) and the Windows partition are only met on hardware.
