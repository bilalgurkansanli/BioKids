# BioKids

**A Debian-based Linux system for children aged 4–10, with parental controls that run
entirely on your own machine.**

*Çocuklar için, tamamen kendi bilgisayarınızda çalışan ebeveyn kontrollü Linux sistemi.*

> ⚠️ Early development. Nothing here is release-ready yet. See
> [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## What it does

- **Time limits** that a child cannot cheat by changing the clock. Daily quotas, time
  windows, separate weekday and weekend rules, spoken warnings before the machine
  shuts down.
- **Content filtering** with an allowlist mode for younger children and a blocklist
  mode for older ones, enforced at the DNS level and in the browser. Blocked pages
  offer an "ask a parent" button that sends a request to the parent's phone.
- **A child launcher** instead of a desktop. Big tiles, no file system, no settings, no
  terminal. The interface adapts to the child's age.
- **Earned time.** Time spent in educational applications turns into extra playtime, at
  a ratio the parent sets.
- **Zio and Sio**, two voice companions who announce warnings and rewards. Optionally,
  they speak in the parents' own recorded voices, cloned locally.
- **Optional AI supervision** that looks at the screen when something new happens,
  scores how appropriate it is for the child's age, and tells the parent. The model
  runs on your GPU; no screenshot ever leaves the machine.
- **A parent panel** you reach from your phone over your own private network. No
  account, no cloud, no port forwarding.

---

## Privacy

**Nothing leaves your device.** No telemetry, no analytics, no crash reporting, no
account. Screenshots are analysed in memory on your own GPU and discarded. Voice
recordings never leave the computer.

The complete list of outbound connections BioKids makes — and there are only eight, most
of them optional — is in [`docs/SECURITY.md`](docs/SECURITY.md#4-outbound-network-inventory).

---

## What it does not do

BioKids is built for a **curious child aged 4 to 10, working alone on one computer.**

It will not stop someone who boots from a USB stick, edits the kernel command line,
removes the drive, or has an adult helping them. It does not protect phones or tablets.
It is a supervision aid, not a guarantee — no filter catches everything, and none of
this replaces talking to your child.

The full list of what is in and out of scope is in
[`docs/SECURITY.md`](docs/SECURITY.md#1-threat-model). We would rather be honest about
the limits than let a parent feel safer than they are.

---

## Install

On an existing Debian 13 system:

```bash
curl -fsSL https://apt.biokids.bilalgurkansanli.com/KEY.gpg \
  | sudo tee /usr/share/keyrings/biokids-archive-keyring.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/biokids-archive-keyring.gpg] \
https://apt.biokids.bilalgurkansanli.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/biokids.list

sudo apt update && sudo apt install biokids
```

Then open `http://127.0.0.1:7878` and follow the wizard.

A bootable ISO with everything preinstalled will come later.

---

## Documentation

| | |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | project constitution and conventions |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | components, time engine, policy engine, data model |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | every design decision and why |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | phases and what is deliberately deferred |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model, privileges, filtering internals |
| [`docs/AI-LAYER.md`](docs/AI-LAYER.md) | the optional on-device supervision worker |
| [`docs/VOICE.md`](docs/VOICE.md) | Zio and Sio |
| [`docs/UX.md`](docs/UX.md) | design tokens and interface rules |
| [`docs/PACKAGING.md`](docs/PACKAGING.md) | packages, APT repo, ISO, CI |

---

## Contributing

Contributions are welcome. Please read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — it includes the list of features this
project will **not** accept, such as covert monitoring, keystroke logging, and live
screen mirroring of a child's session.

---

## License

MIT. See [`LICENSE`](LICENSE).

Built under the [BioStudio](https://bilalgurkansanli.com) label by Bilal Gürkan Şanlı.
