# The ISO

**v1.1, not part of v1.0.** v1.0 is the Debian packages, installed from the project's
APT repository on an existing Debian 13 (D-185, `docs/ROADMAP.md`). `CLAUDE.md` §1 is
explicit: never work on the ISO before the packages work, and this directory stays
empty until v1.0 is released.

The image will be built with Debian's `live-build` and installed with Debian's own
installer, **preseeded**, not with Calamares (D-185). It installs the BioKids packages
from the APT repository, so that repository must exist first. `docs/PACKAGING.md` §3
has the layout this directory will take and the settings `live-build` needs.

Two notes from that section are worth repeating here, because they are the ones that
cost a day each when ignored:

- **Do not develop inside the ISO.** Every `lb build` is 20–40 minutes. Develop against
  a normal Debian VM with the `.deb` installed; the image is a packaging step at the
  end, not an iteration loop.
- **`lb clean --purge` between config changes**, or you will spend an hour debugging a
  stale cache.
