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
│  [avatar] Merhaba Elif     ( 🔉 ━━━●━━ 🔊 )  [Zio]   ◔ 42 dk  │
├──────────────────────────────────────────────────────────────┤
│ (🦒) [Günün hayvanı: Zürafa] Zürafa, dünyanın…  🎁 🐾  [✕]   │  ← §2.7
│                                                              │
│    ┌────────┐   ┌────────┐   ┌────────┐   ┌────────┐         │
│    │      ● │   │        │   │        │   │        │         │
│    │ 🎨     │   │ 🔢     │   │ 🌐     │   │ 🎮     │         │
│    │        │   │        │   │        │   │        │         │
│    │ Boya   │   │ Sayılar│   │ İnternet│  │ Oyunlar│         │
│    └────────┘   └────────┘   └────────┘   └────────┘         │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  ☀ ──────●─────       Açık (🎨)               [ Kapat ]      │
└──────────────────────────────────────────────────────────────┘
```

Fullscreen Chromium kiosk. No window chrome, no scrollbars, nothing behind it.

The sound is at the top right (D-199): 🔉 and 🔊 step by a tenth for a child who
taps, and the bar between them follows a finger dragged or tapped anywhere along it,
or the arrow keys (D-201). At the bottom, **Açık** lists the apps sent to the
background with the app bar's 🏠, each a round button with the app's own icon, and a
dot marks their tiles; a tap on either brings the same window back (D-201). Web tiles
are not listed: each opens a window of its own (D-197).

### 2.2 Age bands

| | 4–7 | 8–10 |
|---|---|---|
| Tile size | 220 × 220 px, max 8 tiles | 150 × 150 px, max 20 |
| Labels | one word, `--font-kid`, 28 px | short phrases, 18 px |
| Spoken labels | yes, on focus/hover | no |
| Clock | none | yes, and a date |
| Remaining time | coloured ring with the minutes inside (D-162) | ring plus minutes |
| File manager | no | yes, home only |
| Own stats | no | yes, simple bar chart |
| Reading required | none | assumed |

Band is derived from age, parent-overridable.

### 2.3 Time ring

Top right, always visible. A circle that empties as time is used, with the minutes
left written inside it and "dk" underneath (D-162). `--ok` above 25%, `--warn` below
25%, `--danger` below 10%, with a slow pulse in the last minute. The number stays dark
whatever the ring's colour, for contrast. A child who cannot read numbers still
understands a shrinking circle; the owner asked for the number for every age, because
the parent standing behind a four-year-old reads it too. The full-screen time warning
(screen 6) shows the same ring and the same number.

### 2.4 Rules

- Every interactive target is at least **64 × 64 px** for 4–7, 44 × 44 px for 8–10.
  One exception: the app bar over a window too narrow for it (§2.6, D-205), whose
  buttons are 48 wide and the title bar's 64 tall, so the app keeps its title bar.
- **No double-click anywhere.** Single tap or click, always.
- Every action gives feedback within 100 ms, even if the app takes 3 seconds to start.
  A tile that does nothing for two seconds will be tapped eight more times.
- **Opening an app happens on its tile** (D-208). From the tap until the app's window
  is in front, the tile has a teal ring breathing around it and "Açılıyor…" where its
  name was; nothing else on the screen changes, and the other tiles keep working. A
  second tap on that tile does nothing, and the launcher never starts a second copy
  of an app that is still opening or already open. The ring goes when the app's
  window takes the screen, after 25 seconds, or when the launcher says no - then the
  calm Blocked screen, as always. Not a spinner and not a screen of its own: a slow
  game kept the child on "Yapboz açılıyor" for many seconds, with nothing to do.
  Without JavaScript the tile leads to the loading screen with the app's icon and an
  "Aç" button (screen 2).
- No modal that a child cannot dismiss. The only unskippable screen is the first-run
  explanation.
- **Every app has a way out a child can find** (D-163, D-197). Over every app
  window the app bar (§2.6) holds a big close button in the top right corner of
  the window - of the screen, for the app that fills it - and it closes the app
  even when the app will not close itself. Each app window fills the screen where
  it can, under a 64-pixel title bar the bar sits in, also when the app shrinks it
  back to a size it saved (D-205); one that cannot, such as a game with a fixed
  size, opens in the middle. No app starts full screen. Whenever the bar is not
  running, the title bar has the close button instead. The launcher itself has no
  title bar and no bar over it; it is never closed, only covered.
- **A tile that cannot work is not drawn.** An app whose program is not installed
  (D-164), or a site served on this machine with nothing behind it yet, such as
  Vikipedi before the encyclopedia is downloaded (D-190).
- `prefers-reduced-motion` is respected everywhere.
- High contrast mode and a font-scale slider live in the parent panel, not the child UI.

### 2.5 Screens

1. **Home** — the tile grid, and above it the day's surprise (§2.7).
2. **Launching** — big icon, app name, "… açılıyor", and where the page runs no
   script an "Aç" button. Only for a launcher without JavaScript and any link that
   still leads here: with JavaScript the home screen opens apps on their tiles (§2.4,
   D-208).
3. **Blocked** — Zio or Sio looking concerned, one sentence ("Burası senin için
   değil"), and a large **"Büyüğüme sor"** button: it names no parent, because the
   request goes to whoever holds the one parent account (D-135). Never a URL, never an
   error code, never the word "forbidden". A site the policy or the DNS filter closed
   lands here too: the extension sends the tab to it instead of Chromium's error page
   (D-187).
   **The kind of app is used up** (D-193) is a second form of the same screen: "Bunun
   bugünlük süresi bitti", a line telling the child to pick something else from the
   home screen, and only a way back — no ask button, because minutes a parent grants
   go to the day, not to games, and a "yes" would not open the app.
   **A search the browser refused** (D-202) is a third: "Bunu arayamayız. İstersen bir
   büyüğüne sorabilirsin." It never repeats the word - the page is not told it - and
   "Büyüğüme sor" files a request that names nothing: the parent reads that the child
   wanted to search something closed and is asked to talk with them.
4. **Break** — a calm full-screen timer, a suggestion ("gözlerini dinlendir"), no
   dismiss button.
5. **Time up** — friendly, a countdown, a note that unsaved work should be saved.
6. **Time warning** — at ten, five and one minute, a short full-screen pause in front
   of whatever app is open (D-160): the speaking character large in the middle, the
   sentence actually being said in a bubble, the ring underneath. Seven seconds,
   then the app comes back. Chosen by the owner over a corner card, which an app
   window simply covered.
7. **First run** — the supervision explanation, age-appropriate, spoken for 4–7. It
   says the family can see which sites the child opens, the site's name and not its
   pages (D-187).
8. **Earned** — a brief celebration when bonus minutes arrive. Keep it to 2 seconds.
9. **Sor Bana** (D-165), when a parent has switched it on — a big text box and a
   send button, and with voice on (D-166) a round microphone button the child holds
   while speaking, which says "Dinliyorum…" while it listens. Under the box, always:
   "Sorduğun bazı soruları ailen görebilir." The answer appears in a bubble and Zio
   reads it aloud. A refusal is a kind fixed sentence in the same bubble, never an
   error. While the model loads, "Biraz bekle, aklımı topluyorum…", then
   "Düşünüyorum…", in words rather than a spinner.

### 2.6 The app bar

Asked for by the owner (D-197, D-201): next to the close button, a way to send an
app to the background, the app's own volume, and the time. It fits the window it
sits on (D-205).

```
                  ┌──────┬──────────┬──────┬─────────────┬──────┬──────┐
   … app title …  │  🏠  │ ⏳ 12 dk │  🔉  │ ━━━━━●━━━━━ │  🔊  │  ✕   │  ← top right of the app
                  │      │  15:24   │      │             │      │      │
                  └──────┴──────────┴──────┴─────────────┴──────┴──────┘

   a narrower window:                       a very narrow one:
   ┌────┬─────────┬────┬────┬────┐          ┌────┬────┐
   │ 🏠 │ ⏳ 12 dk │ 🔉 │ 🔊 │ ✕  │          │ 🏠 │ ✕  │
   └────┴─────────┴────┴────┴────┘          └────┴────┘
```

- Shown whenever an app window is the active one, at the top right of that
  window's own title bar: for an app that fills the screen, the top right of the
  screen. Never over the launcher, its warnings or the screens that end the day. It
  never takes the keyboard from the app.
- In the whole bar each button is 64 × 64 for the finger, a 56-pixel rounded
  square for the eye, on the bar's off-white: the same height as the title bar it
  sits in, with the title bar's own line along its bottom, so over a maximised app
  it sits flush in the title bar and covers only the title.
- **As big as the window allows** (D-205). The whole bar, 544 pixels, over a window
  at least 1000 wide: every app that fills a screen 1024 pixels wide or more. Over
  a narrower one - Frozen Bubble's 640 × 480, a game that cannot fill the screen -
  the compact bar, 284 pixels: buttons 48 wide with 44-pixel faces, still as tall
  as the title bar, the minutes without the clock, and the sound by 🔉 and 🔊
  alone. Under 520 pixels, a small question an app asks, only 🏠 and ✕. So the bar
  never covers much more than half of a window's title bar; for a window partly off
  the screen, half of what is left of it on the screen.
- **🏠 Ana ekran** sends the app to the background and shows the launcher. The app
  keeps running; its tile, or its button under **Açık** at the launcher's bottom,
  brings the same window back, not a second copy.
- **⏳ 12 dk** — the minutes left today, as the launcher's ring counts them; orange
  in the last five minutes, red in the last one. Under it, in the whole bar, the
  clock, for every child (D-201).
- **🔉 / 🔊** turn this app's own sound down or up by a tenth, never the machine's,
  so Zio and Sio stay as loud as they were. In the whole bar the slider between them
  follows a finger dragged or tapped anywhere along the bar's height, with a knob as
  big as a fingertip. All three dim when the app makes no sound. The machine's
  volume is the launcher's.
- **✕ Kapat** closes the app. It asks first, so an app can ask "save your
  drawing?"; an app still there after three seconds, or tapped a second time, is
  stopped. Red, with a white cross, as the title bar's button was.
- **A window being dragged** leaves the bar behind at once and gets it back on its
  title bar a quarter of a second after it stops — never a bar trailing the window.
  An app that filled the screen fills it again when the finger lets go; a game with
  a fixed size stays where it was put.
- **A new window fills the screen** (D-205). Every app window is maximised as it
  opens (D-163); an app that then shrinks it back to the size it saved last time,
  as KDE's apps do, is maximised again by the bar, in its first few seconds and at
  most twice. The bar is not shown on the window until it has its size. After that
  an app's own size is not fought, and nothing is maximised under a finger.
- No words on the buttons; a screen reader hears "Ana ekran", "Sesi kıs", "Sesi
  aç", "Kapat", and the minutes left.

### 2.7 Bugünün sürprizi

Asked for by the owner (D-203): a child who sees the same screen every day gets
bored. One strip between the header and the tiles, new every day, inside a theme
that changes every Monday.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ (🦒)  [Günün hayvanı: Zürafa] Zürafa, dünyanın en   🎁 Bugünün sürprizi    ✕ │
│       uzun boylu hayvanıdır. (8-10: a sentence more) 🐾 Hayvanlar haftası    │
└──────────────────────────────────────────────────────────────────────────┘
```

- Five kinds, dealt in turn: **Biliyor muydun?** (a fact), **Bilmece** (🤔, and a
  "Cevabı gör" button that opens the answer in its place), **Günün hayvanı**,
  **Bugünün kelimesi**, **Bugünün görevi** (a small task; when it names one of the
  child's own tiles, that tile is a button beside it, and a task never names a tile
  the child does not have).
- 4–7: a big picture, one short sentence, and a tap on it is Zio reading it; the
  answer is read when it opens. 8–10: one sentence more, and no voice.
- Compact: at 1280 × 800 the tiles keep their rows under it — two of 220 pixels for
  4–7, three of 150 for 8–10 — so an 8–10 card is at most two lines.
- ✕ puts it away until tomorrow. It is drawn by the server, so everything but ✕
  works with scripts off.
- The theme's colour is a tint of a brand token; the words stay `--ink`.
- What it says is written and checked by people, never generated, and is in
  `biokids_core/data/daily/daily.toml` with the rules for adding to it.

---

## 3. Parent panel

### 3.1 Information architecture

The menu has six entries (`templates/base.html`), which still fit the phone's
bottom bar at 360 px. Above 40rem they are a row at the top, tightened just above
the breakpoint so that the English labels fit at 640 px; with a larger font the row
wraps to a second line rather than letting the page scroll sideways:

```
Bugün / Today          ← default, the only screen most parents open; a badge
                         counts pending requests, and the banner opens İstekler
Çocuklar / Children    → per child: Süre kuralları (time), Uygulamalar ve siteler
                         (D-164), İnternet (D-167), Asistan (D-165), Düzenle (D-191)
Geçmiş / History       → fourteen days of minutes, and one day's apps, sites and
                         requests (D-192)
Analiz / Insights      → a week or a month in plain words, then the numbers (D-206)
Kayıtlar / Audit log
Ayarlar / Settings     → PIN, passphrase, email, phone access, language, and Zio
                         and Sio's voice: the natural voice to download (D-196)
                         and the parent's own (D-174)
```

**İstekler / Requests** is not in the menu: it is one tap from Today, where the badge
and the banner are, because a child is waiting at the computer. A request about a
refused search (D-202) shows no word and offers no "İzin ver", only "Tamam,
konuşacağız".

**İnternet** has, under **Gelişmiş**, the words that cannot be searched (D-202): one
sentence saying BioKids already stops adult and harmful searches with its own list,
a box for the family's own words, one per line, and a line saying the list is the
same for every child, because the browser is one per machine. A word that cannot be
one (a single letter, more than 30 characters, past the hundredth) is left out and
the parent is told.

**Uygulamalar ve siteler** says what an upgrade brought (D-207). A child who already
exists keeps exactly the tiles their parent chose, so a new app reaches them only
when it is ticked. For thirty days after the upgrade each new tile carries a small
**Yeni**, and one line at the top names those that suit the child's age, can start
on this machine and are not ticked yet: "2 yeni uygulama bu çocuğun yaşına uygun:
Patates Kafa, Hafıza." The same line, as a link to the screen, sits under the child
on **Çocuklar**. Saving the screen answers it: a parent who looked and left them
unticked is not asked again, though the **Yeni** stays. A fresh install marks
nothing new, and the child's own screen never changes by itself.

### 3.2 The Today screen

One card per child, and under them one card for the week (D-206):

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

A question the assistant refused (D-165) appears the same way, as a banner that
names the child and the count and opens **Çocuklar → Asistan**, where each one is
listed with the child's words and the reason, and can be marked as seen.

Under the children, **Bu hafta**: for each child at most three of the week's
highlights (§3.9), one sentence each, and "Analizin tamamı →". It is read from the
database, so it is there when the daemon is not answering, and a failure to work it
out costs the card and never the screen. It is not live and needs no glance to be
understood; it is for the evening, not for the child waiting at the computer.

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

Nine steps, each skippable with a sane default, under five minutes total.

1. Language
2. Parent account and PIN
3. Passphrase and the 8-word recovery code (shown once, with a "I wrote it down" check)
4. Add a child: name, birth year, avatar
5. Choose apps from the catalog (pre-selected by age). Volume, brightness and
   shutdown are not offered as boxes: every child has them (D-194).
6. Daily quota and time window (pre-filled by age)
7. Internet: **Açık internet, zararlı siteler kapalı** (the default, D-167) or
   **Sadece seçtiğim siteler**
8. Phone access — four numbered steps that each say when they are done (D-169,
   D-195): connect this computer (one button installs Tailscale and shows its
   sign-in page as a button and a QR code), allow phone access (Tailscale's one
   approval, when it asks), install Tailscale on the phone (App Store and Google
   Play as QR codes; done when a phone joins the tailnet), open the panel on the
   phone (its address, a QR code, and "Ana ekrana ekle")
9. Extras: Zio and Sio's voices, on by default. The assistant is shown, disabled; it
   is switched on per child under **Çocuklar → Asistan** (D-165).

Email is not a step: it is set up under **Ayarlar**, where it can be tested (D-182).
The wizard ends on a closing screen with the child's picture and what is left, as
three numbered steps: restart once so the login screen shows the child (D-155), how
the child logs in, and every way back to the panel. Until the machine has restarted,
**Bugün** shows a banner with "Şimdi yeniden başlat" (D-195).

The parent never has to find the panel (D-170, D-195): installing the package opens
the wizard at once in the desktop that is open, and until setup is complete it opens
again at every login; a "BioKids Ebeveyn Paneli" shortcut is put on the parent's
desktop once, and the panel is in the applications menu. On the login screen each
child is their own picture and name, over Zio and Sio, never the operating system's
logo.

**Yardım / Help** (`/help`) is in the top bar and on the sign-in page: the questions
parents ask first, each answered in the words the screens use. It is open before
signing in, because "PIN'imi unuttum" is one of them.

### 3.5 PWA

`manifest.json`, maskable icon, `display: standalone`, a service worker caching the
shell so the app opens instantly. Never cache live data: with the tailnet unreachable
the parent sees `/offline`, one sentence saying the panel could not be reached,
rendered without the session so the cached copy holds no name and no token (D-194).

### 3.6 A child's details

**Çocuklar → Düzenle** (`/children/{id}/edit`, D-191): name, birth year, picture and
language, and under **Gelişmiş** the supervision indicator — shown, a small icon, or
hidden (the first-run explanation still runs, D-76). The account name is shown, not
editable. Saving a new name or picture updates the login screen.

At the bottom, closed by default, **"Elif adlı çocuğu BioKids'ten kaldır"**: red, behind the
passphrase, and saying plainly what it does — times, apps, requests and history are
deleted and nobody can log in to the account any more — and what it does not: the
account and its files (pictures, drawings) stay, with the command an administrator
would use to delete them too. It is refused while the child is at the computer.

### 3.7 History

**Geçmiş** (`/history`, D-192) answers "what did you do on the computer today?". For one
child at a time (a button per child when there are several): fourteen bars, one per
day, each a link to that day and coloured when the day went over its quota. Under them,
for the chosen day: the apps by time, the sites opened (one line per site, with the
first and last time), and the child's requests with their answers. A note under the
sites says that only the site's name is kept, that the child was told, and that it is
deleted after 30 days. No chart library and no script; a tap is a link.

### 3.8 Limits per kind of app

The time screen (**Süre kuralları**) keeps them under **Gelişmiş** (D-193): minutes a
day for Oyun, Eğitim, İnternet and Serbest, with one line of help ("for example, at
most 30 minutes a day of games"). An empty field is no limit of its own; the day's time
still applies. The child sees a used-up kind as the second form of the Blocked screen
(§2.5).

### 3.9 Insights

**Analiz** (`/insights`, D-206) answers the questions a parent asks over a week: is it
more than before, where does the time go, is there anything I should do? One child at a
time (a button per child, as in History), and the period as three links that read as
one control: **Bu hafta** (the default), **Geçen hafta**, **Son 30 gün**.

```
┌──────────────────────────────────────────┐
│ [ Bu hafta ]  Geçen hafta   Son 30 gün   │
│ 05.10 – 11.10.2026 · bugüne kadar        │
│                                          │
│ Öne çıkanlar                             │
│ ▌📉 Geçen haftanın aynı günlerine göre   │
│ ▌   günlük ortalama ekran süresi %18     │
│ ▌   azaldı.                              │
│ ▌🌙 1 gece, saat 21:00 ile 06:00         │
│ ▌   arasında da kullanıldı.              │
│ ▌   İsterseniz … Kullanım saatleri →     │
│                                          │
│ Ekran süresi                             │
│ [Toplam 4 sa 54 dk] [Günde ortalama 49 dk│
│                      ↓ %18 …]            │
│ ▁▃▅▂▇█▁  (a bar per day, quota dashed)   │
│ Zaman nereye gitti?  ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   │
│ En çok kullanılan uygulamalar            │
│ Günün hangi saatlerinde?  ▁▁▁▃▇▅▁ (24)   │
│ İstekler · En çok açılan siteler         │
└──────────────────────────────────────────┘
```

- **Öne çıkanlar** first, because they are what a parent came for: at most five
  sentences, the most important first, each a fact and never a verdict about the
  child — "Ekran süresinin çoğu oyunlarda geçti", never "çok oyun oynadı". A parent
  may read them out with the child beside them. Some carry one gentle suggestion and
  a link to the setting that acts on it (a games limit, earned time, the allowed
  hours, email notifications); the link opens that setting, with **Gelişmiş** open
  where it lives there (`?more=1`). A suggestion is offered only while it is still
  to be done: no "allowed hours" once any are set (the late-use sentence is then
  only the fact, in a calm tone), no "email notifications" once the alert mail is
  on — the Sunday summary alone does not tell a parent of a request.
- Late use is counted in nights: 22:00 to 00:30 is one night, the small hours
  belonging to the evening before.
- "İstek cevapsız kaldı" counts only requests that expired with nobody answering.
  One still open is "bekliyor", in the present, in the requests tally; it is never
  told as unanswered while the "2 istek bekliyor" banner is on Today.
- Then the numbers, as tiles (two across on a phone, four on a wider screen): total,
  a day on average with its change, days used, days the time ran out, weekdays and
  weekends, minutes earned and added. Today counts in the total but not in an
  average or a comparison, and a line under the tiles says so; a week so far is
  compared with the same days of the week before. There is no comparison until the
  days compared with are at least three (or as many as this side has finished)
  with ten minutes a day on average, and a rise beyond tripling is "iki katından
  fazla", never "%900".
- Bars drawn by the server — the days (for a week, each a link to that day in
  **Geçmiş**; thirty bars on a phone are too thin to tap and are not links; the
  day's quota as a dashed line, warm where the time ran out), the kinds of app as
  one bar in parts with a legend that names each and gives minutes and percent, the
  apps with a thin bar and "↑ daha çok", "↓ daha az", "≈ aynı" or "yeni" ("yeni"
  only for an app not used anywhere in the period before), and the 24 hours of the
  day. Each kind of app has one brand colour wherever it appears, and every bar's
  number is also written. No script, no chart library (D-129).
- The requests by kind with how they were answered, and the sites the browser
  reported, ranked by the days they were opened (at most eight, then "ve 3 site
  daha"), with the note that only the name is kept, for 30 days.
- Before there is anything to show: "Henüz yeterli veri yok. Elif bilgisayarı
  kullandıkça burada bir özet belirecek." and nothing else.

The same highlights, at most three per child and without links, end each child's
part of the Sunday mail (D-177). A mail never names a site. The line under the
title says so when that mail is on, rather than "hiçbir yere gönderilmez".

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
