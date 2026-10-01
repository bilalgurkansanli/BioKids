# UX.md

Design rules for the child launcher (`biokids-shell`) and the parent panel
(`biokids-panel`). Both are HTML, both share the design tokens below, and both must
work offline with no build step and no CDN.

---

## 1. Design tokens

One `tokens.css`, shared. Never hardcode a colour or a size anywhere else.

```css
:root {
  /* brand */
  --bio-coral:   #FF6B5B;   /* Zio */
  --bio-amber:   #FFB020;
  --bio-teal:    #1FB6A6;   /* Sio */
  --bio-indigo:  #4B54C8;

  /* semantic */
  --ok:      #2EA86A;
  --warn:    #F2A32B;
  --danger:  #E04A3F;

  /* surfaces */
  --bg:       #FBF7F2;      /* warm off-white, not clinical grey */
  --surface:  #FFFFFF;
  --ink:      #221E1C;
  --ink-soft: #6B625C;
  --line:     #E7DED5;

  /* geometry: generous, rounded, nothing sharp */
  --r-sm: 10px;  --r-md: 18px;  --r-lg: 28px;  --r-pill: 999px;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 16px; --sp-4: 24px; --sp-5: 40px; --sp-6: 64px;

  --shadow-1: 0 2px 6px rgba(34,30,28,.08);
  --shadow-2: 0 8px 24px rgba(34,30,28,.12);

  --font-ui: "Inter", "Noto Sans", system-ui, sans-serif;
  --font-kid: "Fredoka", "Baloo 2", "Comic Neue", var(--font-ui);
}
```

Fonts are **vendored** as WOFF2 in `assets/fonts/`, never loaded from Google Fonts.
Offline is a hard requirement and a font request is a tracking request.

Art direction: warm, rounded, high contrast, generous whitespace. Deliberately not the
saturated primary-colour clip-art look that most children's software defaults to —
that palette reads as cheap to parents, and parents are half the audience.

---

## 2. Child launcher

### 2.1 Layout

```
┌──────────────────────────────────────────────────────────────┐
│  [avatar] Merhaba Elif           ◔ 42 dk        [Zio badge]  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│    ┌────────┐   ┌────────┐   ┌────────┐   ┌────────┐         │
│    │        │   │        │   │        │   │        │         │
│    │ 🎨     │   │ 🔢     │   │ 🌐     │   │ 🎮     │         │
│    │        │   │        │   │        │   │        │         │
│    │ Boya   │   │ Sayılar│   │ İnternet│  │ Oyunlar│         │
│    └────────┘   └────────┘   └────────┘   └────────┘         │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  🔊 ────────●───    ☀ ──────●─────            [ Kapat ]      │
└──────────────────────────────────────────────────────────────┘
```

Fullscreen Chromium kiosk. No window chrome, no scrollbars, nothing behind it.

### 2.2 Age bands

| | 4–7 | 8–10 |
|---|---|---|
| Tile size | 220 × 220 px, max 8 tiles | 150 × 150 px, max 20 |
| Labels | one word, `--font-kid`, 28 px | short phrases, 18 px |
| Spoken labels | yes, on focus/hover | no |
| Clock | none | yes, and a date |
| Remaining time | coloured ring only, no numbers | ring plus minutes |
| File manager | no | yes, home only |
| Own stats | no | yes, simple bar chart |
| Reading required | none | assumed |

Band is derived from age, parent-overridable.

### 2.3 Time ring

Top right, always visible. A circle that empties as time is used.
`--ok` above 25%, `--warn` below 25%, `--danger` below 10%, with a slow pulse in the
last minute. For 4–7 it is purely visual — a child who cannot read numbers still
understands a shrinking circle.

### 2.4 Rules

- Every interactive target is at least **64 × 64 px** for 4–7, 44 × 44 px for 8–10.
- **No double-click anywhere.** Single tap or click, always.
- Every action gives feedback within 100 ms, even if the app takes 3 seconds to start.
  A tile that does nothing for two seconds will be tapped eight more times.
- Launching an app shows a full-screen loading state with the app's icon, not a spinner.
- No modal that a child cannot dismiss. The only unskippable screen is the first-run
  explanation.
- `prefers-reduced-motion` is respected everywhere.
- High contrast mode and a font-scale slider live in the parent panel, not the child UI.

### 2.5 Screens

1. **Home** — the tile grid.
2. **Launching** — big icon, app name, a progress bar.
3. **Blocked** — Zio or Sio looking concerned, one sentence, and a large
   **"Anneme sor" / "Babama sor"** button. Never a URL, never an error code, never the
   word "forbidden".
4. **Break** — a calm full-screen timer, a suggestion ("gözlerini dinlendir"), no
   dismiss button.
5. **Time up** — friendly, a countdown, a note that unsaved work should be saved.
6. **Time warning** — at ten, five and one minute, a short full-screen pause in front
   of whatever app is open (D-160): the speaking character large in the middle, the
   sentence actually being said in a bubble, the ring underneath. Seven seconds,
   then the app comes back. Chosen by the owner over a corner card, which an app
   window simply covered.
6. **First run** — the supervision explanation, age-appropriate, spoken for 4–7.
7. **Earned** — a brief celebration when bonus minutes arrive. Keep it to 2 seconds.

---

## 3. Parent panel

### 3.1 Information architecture

```
Bugün / Today          ← default, the only screen most parents open
Çocuklar / Children    → per child: time, apps, filter, AI, voice
İstekler / Requests    ← badge when pending; this must be reachable in one tap
Geçmiş / History       → usage charts, sites, alerts
Sesler / Voices        → Zio & Sio, recording
Ayarlar / Settings     → PIN, email, Tailscale, language, updates, data
Kayıtlar / Audit log
```

### 3.2 The Today screen

One card per child, and nothing else:

```
┌────────────────────────────────────────────┐
│  [avatar]  Elif                    ● açık  │
│                                            │
│       ◕  42 dk kaldı                       │
│       şu an: TuxPaint (Eğitim)             │
│                                            │
│   [ +10 dk ]  [ Kilitle ]  [ Kapat ]       │
└────────────────────────────────────────────┘
```

Three buttons, large, thumb-reachable at the bottom of a phone screen. Live-updating
via server-sent events. A parent glancing at this on their phone must understand it in
three seconds without reading a label twice.

Pending access requests appear as a banner above the cards, because they are
time-sensitive — a child is standing at the computer waiting.

### 3.3 Complexity rules

- Any screen with more than **seven** controls needs a "Gelişmiş / Advanced"
  disclosure, with everything rare hidden inside it.
- Every setting has a one-line plain-language explanation under it. Not a tooltip —
  parents on phones do not hover.
- Destructive actions are red, need the passphrase, and state the consequence
  ("Elif'in tüm geçmişi silinecek").
- Never show a raw value a parent cannot act on: no seconds, no timestamps in ISO
  format, no domain lists longer than 10 without search.
- Time is always in minutes and hours in the UI, seconds only in exports.

### 3.4 The setup wizard

Ten steps, each skippable with a sane default, under five minutes total.

1. Language
2. Parent account and PIN
3. Passphrase and the 8-word recovery code (shown once, with a "I wrote it down" check)
4. Add a child: name, birth year, avatar
5. Choose apps from the catalog (pre-selected by age)
6. Daily quota and time window (pre-filled by age: 1 h for 4–7, 2 h for 8–10)
7. Filter mode (pre-selected by age: allowlist for 4–7, blocklist for 8–10) and the
   optional recommended blocklist
8. Phone access — Tailscale, with a QR code
9. Email notifications — SMTP, with a Gmail app-password walkthrough
10. Optional extras: voices, AI supervision. Both explained honestly, both default off
    for AI and on for stock voices.

Ends with a summary card and a "test it now" button that starts a 2-minute demo session.

### 3.5 PWA

`manifest.json`, maskable icon, `display: standalone`, a service worker caching the
shell so the app opens instantly and shows the last known state when the tailnet is
briefly unreachable. Never cache live data.

---

## 4. Writing rules

- **Turkish is the primary voice of the product.** Write Turkish first, translate to
  English, not the reverse. The Turkish must not read like a translation.
- Address the parent as **siz**, the child as **sen**.
- No jargon in the parent panel: not "DNS", not "policy", not "daemon". Say "site
  engelleme", "kurallar", "BioKids servisi".
- Error messages say what happened, why, and what to do. Three short sentences maximum.
- Never blame the child in text the child can see. "Bu sayfa senin için değil", not
  "Yasak siteye girmeye çalıştın".
- Never blame the parent either. "Ayar kaydedilemedi, tekrar deneyin" beats "Geçersiz
  giriş".

---

## 5. Accessibility

- WCAG AA contrast minimum everywhere; AAA on the child launcher.
- Full keyboard navigation with a clearly visible focus ring.
- Proper landmarks and labels; the launcher is usable with a screen reader.
- A dyslexia-friendly font option (OpenDyslexic, vendored) in the parent panel.
- A font-scale slider from 100% to 175%, applied to the child launcher.
- Colour is never the only signal: the time ring also changes thickness, and warning
  banners carry an icon.
