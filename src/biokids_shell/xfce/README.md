# The child's XFCE session

XFCE is the substrate, not the interface: it holds the session together and the
child never sees it (D-13). These files are what make that true. They are
installed to `/usr/share/biokids/xfce/` and copied into a child's home the first
time their account is created.

Everything here is a *configuration* change. Nothing patches XFCE, nothing
replaces a binary, and `biokids-harden --undo` is not needed to reverse any of
it: delete the files and log in again.

## What each file does

| File | Effect |
|---|---|
| `xfwm4.xml` | A 64-pixel title bar with one big close button (D-163) - which the launcher takes away while the app bar is running and gives back when it is not (D-197) - windows opened in the middle of the screen, no keyboard shortcut that closes or moves a window, no workspace switching, no alt-tab |
| `xfce4-session.xml` | No session save, no logout dialog, no "restore on login" prompt |
| `xfce4-desktop.xml` | No desktop icons, no right-click desktop menu, a plain background |
| `xfce4-power-manager.xml` | No suspend on lid or idle — a suspended machine mid-countdown is a machine that comes back in a state nobody expects |
| `biokids-shell.desktop` | Autostarts the launcher's user unit in the session |

## What XFCE cannot do, and what does it instead

| Escape | Handled by |
|---|---|
| `Alt+F4`, `Alt+Space`, `Super` | `xfwm4.xml` — the shortcuts are unbound, so nothing receives them |
| `Ctrl+Alt+F1…F6` (virtual terminals) | `etc/systemd/logind.conf.d/biokids.conf` and `etc/X11/xorg.conf.d/50-biokids-no-vt-switch.conf`, both shipped by `biokids-shell` |
| A terminal emulator | None is installed. `biokids-desktop` does not depend on one and `biokids-harden --check` reports it if one appears |
| `Ctrl+W`, `Ctrl+T`, `F11`, `F12` in the browser | The kiosk profile's managed policy plus `shell.js`; see `kiosk.py` |
| Booting another OS from a USB stick | **Nothing here.** Firmware territory, out of scope, documented in `docs/SECURITY.md` §1 |

That last row is the honest one. A ten-year-old with a phone and a search engine
gets past all of this, and the README says so rather than letting a parent feel
safer than they are.

## Applying it by hand

```bash
mkdir -p ~/.config/xfce4/xfconf/xfce-perchannel-xml
cp /usr/share/biokids/xfce/*.xml ~/.config/xfce4/xfconf/xfce-perchannel-xml/
mkdir -p ~/.config/autostart
cp /usr/share/biokids/xfce/biokids-shell.desktop ~/.config/autostart/
```

Log out and back in; xfconf reads these at session start and will overwrite them
on exit if the session is running while they are copied.
