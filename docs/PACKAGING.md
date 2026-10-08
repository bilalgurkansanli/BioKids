# PACKAGING.md

How BioKids is built, signed, published and installed.

---

## 1. Debian packages

**One source package, several binary packages.** `debian/control` carries multiple
`Package:` stanzas.

| Binary package | Contents | Depends |
|---|---|---|
| `biokids-core` | shared Python lib, DB schema, i18n, app catalog | python3, python3-sqlalchemy, alembic |
| `biokids-daemon` | the enforcement daemon + systemd units, the children's DNS filter | biokids-core, systemd, dnsmasq-base (never `dnsmasq`, D-167), nftables |
| `biokids-panel` | FastAPI panel, templates, static, PWA | biokids-core, python3-fastapi, uvicorn |
| `biokids-shell` | child launcher + kiosk supervisor, the browser extension's source (packed on the machine, D-187), the VT guards, the login-by-picture PAM service | biokids-core, chromium, chromium-l10n, light-locker, devilspie2, xdotool |
| `biokids-voice` | Zio & Sio: espeak-ng, MBROLA or Piper (D-137); `biokids-voice-setup.service`, which downloads Piper and its Turkish voice on the parent's button, never shipping either (D-196) | biokids-core, espeak-ng, pipewire; Suggests mbrola-tr1, mbrola-tr2 |
| `biokids-desktop` | metapackage: XFCE config, theme, LightDM, app set | the above + xfce4 + the catalog apps |
| `biokids-ai` | the assistant (D-165), its setup tool, the offline Wikipedia service (D-190) | biokids-core, llama.cpp-tools; Recommends kiwix-tools, whisper.cpp-tools. **Suggests**, never Depends |
| `biokids-archive-keyring` | our APT signing public key | — |
| `biokids` | metapackage pulling core, daemon, panel, shell, voice | — |

Build with `debhelper` compat 13 and `dh-python` / `pybuild`.

### maintainer scripts

Keep them minimal; every line is a chance to break someone's system.

- `postinst`: create the `biokids` group and the `biokids-ai` user, create
  `/var/lib/biokids` with correct modes, run `alembic upgrade head`, enable and start
  units. biokids-shell's postinst packs and signs the browser extension with a key made
  on the machine (`biokids-admin pack-extension`, D-187).
- `prerm`: stop units.
- `postrm purge`: remove `/var/lib/biokids` **only on purge**, and ask first via
  debconf. A `remove` must never destroy a family's history.
- Never modify a config file the admin may have touched without `ucf`.
- Generated files (`/var/lib/biokids/dns/*`, chromium policies, nftables) are
  **not** conffiles. They are regenerated from the DB and are documented as
  hand-edit-hostile with a header comment.

### Versioning

Semantic versioning, `X.Y.Z`, with a Debian revision (`0.1.0-2` is the v1.0 release
candidate). Tags are `vX.Y.Z`. v1.0 is the packages from the APT repository; the ISO
is v1.1 (D-185). `release.yml` refuses a tag that does not match the upstream version
at the top of `debian/changelog`: `0.1.0-2` can only be tagged `v0.1.0`, and a
`v1.0.0` tag needs a `1.0.0-1` changelog entry first.

---

## 2. APT repository

Built with **`aptly`**, hosted on **GitHub Pages**, served from a custom domain from
day one so the URL never has to change.

```
https://apt.biokids.bilalgurkansanli.com/
  dists/trixie/{main}/binary-amd64/
  pool/main/b/biokids/
  KEY.gpg
```

Distributions: `trixie` (stable releases) and `trixie-testing` (pre-releases from
`main`). Nobody is pointed at `trixie-testing` by default.

Installation, as documented in the README:

```bash
curl -fsSL https://apt.biokids.bilalgurkansanli.com/KEY.gpg \
  | sudo tee /usr/share/keyrings/biokids-archive-keyring.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/biokids-archive-keyring.gpg] \
https://apt.biokids.bilalgurkansanli.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/biokids.list

sudo apt update && sudo apt install biokids
```

**The signing key never touches the repository or a developer machine.** The private
key lives in GitHub Secrets; only the release workflow can use it. Generate a dedicated
key for this, not a personal one, and publish the fingerprint in the README so people
can verify.

This APT repo matters more than the ISO. It is what lets someone adopt BioGuard on an
existing install in two commands, and what lets us ship a fix without rebuilding an
image.

---

## 3. ISO

**v1.1 only (D-185). Do not start this before v1.0 is released.**

Built with Debian `live-build` under `iso/`, and installed with Debian's own installer,
preseeded — not Calamares:

```
iso/
├── auto/config              # lb config invocation
├── config/
│   ├── package-lists/       # biokids.list.chroot, desktop.list.chroot
│   ├── archives/            # our APT repo + key, so packages come from our repo
│   ├── includes.chroot/     # branding, wallpapers, default configs
│   ├── hooks/               # live/ and normal/ hooks
│   └── bootloaders/         # GRUB + isolinux theming
└── Makefile
```

Key settings: `--distribution trixie`, `--architectures amd64`,
`--debian-installer live` with a preseed file, `--archive-areas "main contrib
non-free-firmware"` (non-free-firmware is necessary for wifi on real hardware).

### Practical notes

- Every `lb build` takes 20–40 minutes. **Do not develop inside the ISO.** Develop
  against a normal Debian VM with the `.deb` installed; the ISO is a packaging step at
  the end, not an iteration loop.
- `lb clean --purge` between config changes, or you will debug a stale cache for an hour.
- Target size under 2.5 GB. AI models are never bundled; `biokids-ai-setup` downloads
  them post-install.
- Test order: VirtualBox BIOS → VirtualBox UEFI → a real USB stick on real hardware.
  The first two passing tells you almost nothing about the third.

### Secure Boot

We ship no custom kernel and no out-of-tree modules, so Debian's signed `shim` and
`grub-efi-amd64-signed` should chain-load unmodified. Verify this on real hardware
before claiming support. If it fails, document "disable Secure Boot" and move on — a
signing key is not worth it at this stage.

---

## 4. Hosting artifacts

| Artifact | Primary | Notes |
|---|---|---|
| `.deb` packages | our APT repo | also attached to the GitHub Release |
| ISO | **archive.org** | free, unmetered, permanent; normal practice for distros |
| ISO mirror | GitHub Release | only when the file is under 2 GB |
| `SHA256SUMS` + `.asc` | GitHub Release | always, for every artifact |
| Release notes | GitHub Release | always |

GitHub caps release assets at 2 GB per file, which the full ISO will approach. Publish
the ISO to an archive.org item named `biokids-<version>`, and let the GitHub Release
carry the checksums and a link. Cloudflare R2 is a reasonable later addition if a
download link on the project's own domain is wanted.

---

## 5. CI

GitHub Actions: `ci.yml` and `release.yml` exist; `iso.yml` comes with v1.1.

**`ci.yml`** — on every push and PR:
`ruff check` → `ruff format --check` → `mypy --strict` on core and daemon → `pytest`
with coverage → build all `.deb` in a `debian:trixie` container →
`lintian --fail-on error` → upload the packages as artifacts.

**`release.yml`** — on tag `v*`:
build → sign with the key from Secrets → `aptly publish` → push to the `gh-pages`
branch → create the GitHub Release with the `.deb`, `SHA256SUMS` and the signature →
generate release notes from Conventional Commits.

**`iso.yml`** — manual dispatch and weekly on `main` (v1.1 onwards; not written yet):
`live-build` in a privileged container → smoke-test the boot with QEMU → upload as an
artifact. Not attached to releases automatically; ISO publication is deliberate.

**`ai-eval.yml`** — not built: the risk scorer it would have evaluated went with the
screen watcher (D-180).

### Guards

- Branch protection on `main`: CI must pass, one review (even self-review) required.
- `lintian` errors fail the build. Debian packaging quality is part of the point.
- Dependabot for Python dependencies, grouped weekly.
- A license check step that fails if any dependency's license is not in the allowlist
  (MIT, BSD, Apache-2.0, ISC, PSF). This is how D-30 stays true over time rather than
  being a good intention.

---

## 6. Repository hygiene for an open source project

Set these up before the repo goes public. They are the difference between a project
people trust and one they scroll past.

- `README.md`: what it is, a screenshot of the launcher and the panel, the honest
  threat-model statement, install in two commands, a demo video link.
- `LICENSE` — MIT.
- `CONTRIBUTING.md` — dev setup, the VM workflow, commit conventions, **and the
  explicit list of features we will not accept** (see `SECURITY.md` §7).
- `CODE_OF_CONDUCT.md` — Contributor Covenant.
- `SECURITY.md` in the root — disclosure policy and contact.
- `CHANGELOG.md` — Keep a Changelog format, generated from commits.
- Issue templates: bug, feature, hardware compatibility report.
- A PR template with a checklist: tests, docs, `DECISIONS.md` updated if a decision
  changed.
- `.github/FUNDING.yml` if wanted.
- Topics: `linux`, `debian`, `parental-controls`, `kids`, `privacy`, `fastapi`,
  `self-hosted`.
- A pinned "Roadmap" issue mirroring `docs/ROADMAP.md`.
- Screenshots in `assets/screenshots/`, referenced from the README. A parental-control
  project with no screenshots gets no stars.
