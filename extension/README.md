# The Chromium extension

**Phase 1.** Nothing here yet.

`docs/ARCHITECTURE.md` §2 describes what it will be: an extension force-installed
into the child's Chromium profile by managed policy, reporting
`{child, url, title, timestamp}` for every committed navigation to the daemon over
`127.0.0.1` with the per-install token in `/etc/biokids/ext.token`.

Reading navigations from the browser is far more reliable than parsing window titles,
and it is how two later features work at all: the daemon learns that a domain is new
(which is what triggers the AI layer, `docs/AI-LAYER.md` §3) and per-domain reporting
has something to report.

The token already exists — `biokids-core.postinst` generates it — so the extension has
a credential waiting for it.

## What it will not do

It reports the address of a page, not its contents. No keystrokes, no form data, no
screenshots, no page text. `docs/SECURITY.md` §7 is the list, and this extension is
exactly the sort of component that would be asked to grow past it.
