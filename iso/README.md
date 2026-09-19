# The ISO

**Phase 3. Do not start this early.** `CLAUDE.md` §1 is explicit: never work on the
ISO before the packages work.

`docs/PACKAGING.md` §3 has the layout this directory will take and the settings
`live-build` needs. Two notes from that section are worth repeating here, because they
are the ones that cost a day each when ignored:

- **Do not develop inside the ISO.** Every `lb build` is 20–40 minutes. Develop against
  a normal Debian VM with the `.deb` installed; the image is a packaging step at the
  end, not an iteration loop.
- **`lb clean --purge` between config changes**, or you will spend an hour debugging a
  stale cache.

The APT repository matters more than this directory does. With a repository, someone
adopts BioGuard on an existing Debian install in two commands and gets fixes without
rebuilding an image.
