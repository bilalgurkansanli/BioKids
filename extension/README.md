# The Chromium extension

A Manifest V3 extension in plain JavaScript: `manifest.json` and `background.js`,
no build step, no libraries, no remote code. Chromium's managed policy installs it
in every profile on the machine and nobody can remove it.

## What it does

**The block page.** When a top-level navigation fails because BioKids closed the
site, the tab goes to the launcher's own page instead of a Chromium error:
`<launcher>/blocked?url=<the address, URL-encoded>`. Two errors count:

| Error | Why |
|---|---|
| `net::ERR_BLOCKED_BY_ADMINISTRATOR` | the managed policy's `URLBlocklist` closed it |
| `net::ERR_NAME_NOT_RESOLVED` | the children's DNS filter answered NXDOMAIN (`docs/SECURITY.md` §5) |

Only `http` and `https`, only the tab's main frame, and never an address on the
machine itself, so a launcher page that fails cannot send the tab round in a loop.
With no launcher to ask, Chromium's own error page stays.

**The visit report.** When a page commits in a tab's main frame, the launcher is
told the site's host name with `POST <launcher>/visit` and the body
`{"host": "www.example.com"}`. At most once per host every ten minutes; the times
are kept in `chrome.storage.session`, so a service worker that Chromium stopped and
restarted does not send them again. Addresses on the machine are never reported.

**Finding the launcher.** Each child's launcher listens on
`127.0.0.1:(7879 + uid % 100)`, and nftables lets a child's processes reach only
their own. The extension cannot know the uid, so it asks `GET /healthz` on every
port from 7879 to 7978 at once, with a short timeout, and keeps the first that
answers JSON with `"biokids": "launcher"`. The origin is cached for the browser
session; a request to it that fails starts a new search. The range and the
contract are constants at the top of `background.js`.

## What it never does

- It reports a **host name**, nothing more: never the path, the query, the
  title, the page's contents, a keystroke, a form field or a screenshot.
  `docs/SECURITY.md` §7 is the boundary, and `tests/test_extension.py` fails if
  the request body becomes anything other than `{ host }`.
- No content scripts, so it cannot read or change any page.
- No `tabs` permission: it can send a tab to the block page, but cannot read
  the address or title of any tab.
- It talks to nothing but `127.0.0.1`. Its only host permission is
  `http://127.0.0.1/*`.
- It runs no code from anywhere else and loads nothing at run time.

## How it is installed

An extension that does not come from the Chrome Web Store can only be
force-installed as a CRX signed with the key its id is derived from. No private
key is in this repository: every machine makes its own when BioKids is installed.

```
biokids-admin pack-extension [--source DIR] [--out DIR] [--key PATH]
```

| | Default |
|---|---|
| `--source` | `/usr/share/biokids/extension`: this directory, installed |
| `--key` | `/var/lib/biokids/extension-key.pem`: RSA 2048, 0600 root, made on first use |
| `--out` | `/var/lib/biokids-ext`: 0755, so the child's Chromium can read it |

It writes three files into `--out`, each atomically and 0644:

- `biokids.crx`: the extension, signed as CRX3
- `updates.xml`: the update manifest, pointing at `file:///…/biokids.crx`
- `extension-id`: the 32-letter id this machine's key gives

The managed policy then force-installs it:

```json
"ExtensionInstallForcelist": ["<extension-id>;file:///var/lib/biokids-ext/updates.xml"]
```

The packer is `biokids_core.extension_pack`. The same source and the same key
always give byte-for-byte the same CRX, and `README.md` is not packed.

### Updating it

Chromium installs a new CRX only when its version is higher. **Raise `version`
in `manifest.json` whenever anything in this directory changes**, or machines keep
the old one. The manifest's `update_url` names the same `updates.xml` the policy
does, so Chromium's periodic update check finds the new version there.

A lost key changes the extension id on the next pack. That is harmless as long as
the policy is written from `extension-id` rather than from a remembered value.

## The launcher's side

The extension assumes the launcher provides:

- `GET /healthz` answering JSON that contains `"biokids": "launcher"`
- `GET /blocked?url=…`, the page a child sees for a closed site
- `POST /visit` taking `{"host": "…"}` as JSON

The extension trusts whichever launcher it can reach. nftables keeps a child's
browser to their own port, but the policy covers every profile on the machine,
the parent's included, so the launcher should only answer connections from its
own child's uid.

## Developing

`chrome://extensions` is closed in the children's kiosk. In an ordinary
Chromium profile, turn on developer mode and load this directory unpacked; the
service worker's console is on the same page. A launcher on 127.0.0.1 in the
probe range is needed for anything to happen.
