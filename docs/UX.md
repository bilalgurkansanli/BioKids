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
│  [avatar] Merhaba Elif [⭐ 7] ( 🔉 ━━━●━━ 🔊 )  [Zio] ◔ 42 dk │  ← ⭐ §2.8
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
│  ☀ ──────●─────     Açık (🎨)    [👥 Oturumu değiştir] [ Kapat ]│
└──────────────────────────────────────────────────────────────┘
```

Fullscreen Chromium kiosk. No window chrome, no scrollbars, nothing behind it.

The sound is at the top right (D-199): 🔉 and 🔊 step by a tenth for a child who
taps, and the bar between them follows a finger dragged or tapped anywhere along it,
or the arrow keys (D-201). At the bottom, **Açık** lists the apps sent to the
background with the app bar's 🏠, each a round button with the app's own icon, and a
dot marks their tiles; a tap on either brings the same window back (D-201). Web tiles
are not listed: each opens a window of its own (D-197).

**Oturumu değiştir** (D-227), beside Kapat, asks once ("Başkası mı girecek?"), then
ends the child's session and brings the login screen for a sibling or a parent. While
an app is in front the launcher is covered: around a window that cannot fill the screen
a tap brings the app back, and the cover's 🏠 goes home.

The tiles sit in rows of one size, as many as the width holds (six at 1600 pixels
for 4–7), and every row is centred: a last row that does not fill sits in the
middle under the others, never with holes on the right, because a half-empty row
looked to the owner like something was missing (D-210). More rows than fit scroll
inside the grid, under a header and a footer that stay put. The browser's start
page (`/start`), the child's own sites, is laid out the same way, seven a row at
1600 pixels with the young band's tiles at 200 pixels there.

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
  window takes the screen, after 25 seconds, or when the launcher says no or the app
  ends as it starts (it is looked for two seconds after the launcher's yes) - then the
  calm Blocked screen, as always, but only while that ring is still on the tile: a no
  that comes after the 25 seconds, or after an app's window took the screen, is about
  nothing the child can see and shows nothing. Not a spinner and not a screen of its
  own: a slow game kept the child on "Yapboz açılıyor" for many seconds, with nothing
  to do. Without JavaScript the tile leads to the loading screen with the app's icon,
  its name and an "Aç" button (screen 2).
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
- Text size, high contrast and the reading font are chosen for each child in the
  parent panel (**Çocuklar → Düzenle → Ekran**, D-217), never on the child's screen.
  They change the launcher and the browser's start page; at the largest size the
  tiles grow less than the words in them, so a row holds fewer and the grid scrolls,
  and nothing is wider than the screen.

### 2.5 Screens

1. **Home** — the tile grid, and above it the day's surprise (§2.7).
2. **Launching** — big icon and app name; "… açılıyor" under them only where the
   page's script runs and is opening the app, and where it runs none an "Aç" button
   instead - nothing is opening until it is pressed, and a child who cannot read yet
   should not have a sentence above it. Only for a launcher without JavaScript and any
   link that still leads here: with JavaScript the home screen opens apps on their
   tiles (§2.4, D-208).
3. **Blocked** — Zio or Sio looking concerned, one sentence ("Burası senin için
   değil"), and a large **"Büyüğüme sor"** button: it names no parent, because the
   request goes to every parent and any of them may answer it (D-135, D-218). Never a URL, never an
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
   **Good night** (D-214) is its evening form, when the day's time runs out or its
   last allowed hours close at 19:00 or later: the night's dark blue, Zio sleepy,
   "İyi geceler, Elif." and "Yarın görüşürüz.", with the same end-action sentence and
   the same countdown, in blue rather than red. Zio says it (the daemon's
   good-night line), for every age, as the time-up line always was.
6. **Time warning** — at ten, five and one minute, a short full-screen pause in front
   of whatever app is open (D-160): the speaking character large in the middle, the
   sentence actually being said in a bubble, the ring underneath. Seven seconds,
   then the app comes back. Chosen by the owner over a corner card, which an app
   window simply covered. In an evening's last ten minutes (D-214) the pause is in
   the night's colours, calm at every minute - no amber, no red, no pulse - with Zio
   sleepy and the night's words ("Akşam oldu Elif. 10 dakika sonra yatma vakti."),
   and the screen grows a little darker in four steps, to 60% of the child's own
   brightness; it is put back the next time the launcher can.
7. **First run** — the supervision explanation, age-appropriate, spoken for 4–7. It
   says the family can see which sites the child opens, the site's name and not its
   pages (D-187).
8. **Earned** — a brief celebration when bonus minutes arrive. Keep it to 2 seconds.
9. **Sor Bana** (D-165), when a parent has switched it on — a big text box and a
   send button, and with voice on (D-166) a round microphone button the child holds
   while speaking, which says "Dinliyorum…" while it listens. Under the box, always:
   "Sorduğun bazı soruları ailen görebilir." The answer appears in a bubble a sentence
   at a time, as the model writes it, and Zio reads each sentence aloud in turn, never
   two at once (D-216). A refusal is a kind fixed sentence in the same bubble, never an
   error; an answer stopped part-way keeps what was shown and ends with one ("Bu konuda
   sana bu kadarını anlatabilirim. Gerisini ailenden bir büyüğe sorabilirsin."). Until
   the first sentence, "Düşünüyorum…", and while the model loads, "Biraz bekle, aklımı
   topluyorum…", in words rather than a spinner.
10. **Kapat** (D-100) — "Bilgisayarı kapatalım mı?", **Evet, kapat** and **Vazgeç**.
    The yes is answered at once (D-209): "Bilgisayar kapanıyor…" and "Yarın
    görüşürüz.", said aloud for 4–7, and a few seconds later the computer switches
    off, once the child's open apps have had their chance to save. While anyone else
    is logged in - a parent who switched user, a sibling - the computer stays on and
    only the child's own session ends: "Bilgisayarda başka biri de var, o yüzden
    bilgisayar açık kalıyor. Senin oturumunu kapatıyorum." Nobody is named. When the
    launcher cannot reach BioKids: "Bilgisayar kapanamadı. Bir büyüğüne söyle." and a
    way back. Never the home screen as if nothing had been pressed.
11. **Çıkartma albümüm** (D-211) — the stickers the child earned by learning, this
    week's progress, and their own tiles that earn one (§2.8).
12. **Resimlerim** (D-212) — the pictures the child made, as a grid and one at a time
    (§2.9). A built-in tile a parent ticks like any other.

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
  clock, for every child (D-201). When the launcher cannot say the minutes - the
  daemon restarting during an upgrade - they are not shown, and their box goes
  where nothing else is left in it: the compact bar is then 🏠 🔉 🔊 ✕, with no gap
  where the minutes were. The bar keeps its corner as it narrows and widens.
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
  as KDE's apps do, is maximised again by the bar the moment the window is still,
  in its first few seconds and at most twice. The bar is not shown on the window
  until it has its size: the child may see the app fill the screen, shrink for a
  moment and fill it again, never the bar on the small window in between. (Should
  the window manager not report the shrinking, the bar looks again a second and a
  half after the window appeared, and is on the small window until then.) After
  that an app's own size is not fought, and nothing is maximised under a finger.
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
- Fifteen themes, one a week, so a theme comes back every fifteen weeks: the first
  twelve of D-203, then **İnternette güvenlik** (what to do and whom to tell, never
  what could go wrong), **Duygularımız** (naming a feeling and what helps) and
  **Meslekler** (D-213).

### 2.8 Çıkartma albümüm

Asked for by the owner (D-211): a reward for learning that is never screen time. Each
day with at least 15 minutes in the child's learning tiles - the category as the parent
set it, the same minutes History shows - earns that day's sticker: a cheerful picture
with a name, the same for the whole family that day, a different one tomorrow, and no
sticker twice until all 64 have come.

```
┌──────────────────────────────────────────────────────────────┐
│ [🏠 Ana sayfa]  📖 Çıkartma albümüm                    [⭐ 7] │
│ ┌ Bu hafta ────────────────────────────────────────────────┐ │
│ │  Pzt    Sal    Çar    Per    Cum    Cmt    Paz            │ │
│ │  (🍒)   ( )    (◔?)   ( )    ( )    ( )    ( )            │ │
│ │  Bu hafta bir çıkartma kazandın! 6 dakika daha öğrenme    │ │
│ │  oyunu oynarsan bugünün çıkartmasını kazanırsın. 🔊        │ │
│ └──────────────────────────────────────────────────────────┘ │
│ Çıkartmalarım                                                │
│  [ 🍒 Kiraz ]  [ 🌙 Ay ]  [ 🎺 Trompet ]  …    (newest first) │
│ Çıkartma kazandıran oyunların   (🧩 GCompris) (🔢 Sayılar)    │
└──────────────────────────────────────────────────────────────┘
```

- **The chip** sits beside the child's name on the home screen: ⭐ and the number,
  which a child who cannot read still counts. It is shown once there is a sticker,
  or a tile that can earn one; a tap opens the album.
- **This week**, Monday to Sunday, as seven circles: a sticker where one was earned,
  a dashed circle for a day that passed without, today as a circle filling up with
  a ? inside, the days still to come faint. Under it one or two sentences a parent
  can read out to a five-year-old: the week ("Bu hafta 2 çıkartma kazandın!"), then
  today: earned, how many minutes more (rounded up), or how to start.
- **The stickers**, newest first, big, with their names and dates; today's framed.
  Empty: Zio and "Albümün seni bekliyor!", and what earns one.
- **The child's own learning tiles**, at most four, so "öğrenme oyunu" means
  something to a child who cannot read it.
- **The day's sticker is celebrated once** - not on every load, and not again after a
  restart or a second login: the first page or poll that finds it shows Zio, happy,
  with the sticker and "Yaşasın! Bugünün çıkartmasını kazandın: Kiraz!", above the
  footer, and Zio says it, for either band. Usually the child earns it in an app;
  when the launcher comes back in front it asks, the chip takes the new number, and
  the celebration happens then. It goes by itself after twelve seconds, or at ✕.
- 4-7 hear a sticker's name and the week's sentences on a tap (D-175). Everything
  but the celebration's going away reads with scripts off. A sticker is never
  minutes, and nothing about it leaves the machine.

### 2.9 Resimlerim

Asked for by the owner (D-212): the child sees the pictures they made. A built-in
tile, suggested for every age; it opens a page of the launcher, with no process and
no opening ring.

- From where the drawing programs save: Tux Paint's `~/.tuxpaint/saved/` (not its
  thumbnails' folder), and the child's Pictures folder, `xdg-user-dir PICTURES`,
  `~/Resimler` or `~/Pictures`. Their own top level only, newest first, at most 500.
- **The grid**: big thumbnails (260 pixels for 4-7, 220 for 8-10; the pictures
  themselves, scaled by the browser), the date on each for 8-10. A tap opens one.
- **One picture** as big as the screen on a dark background, with ‹ (newer) and ›
  (older) at the sides and a red ✕ at the top right, each 84 pixels, and "2 / 7"
  under it. All links, so it works without a script; with one, the arrow keys,
  Escape and a sideways swipe do the same.
- Empty: Zio, "Henüz resim yok" and "Boya'da bir resim yapınca burada görünecek.",
  said aloud for 4-7, with a button to Boya (or Çizim) when the child has it.
- Under the grid, always: "Resimlerin bu bilgisayarda kalır, kimseye gönderilmez."
  The parent's panel never shows them.

---

### 2.10 What the family touches added (D-229 to D-241)

- **The one-minute warning** carries "🏁 Bitireyim +3 dk" when a last round is due
  (D-230): green, under the ring, read aloud for 4–7; tapped, the sentence becomes "Son
  tur! 3 dakika daha" and the screen holds a moment longer. The home screen's footer has
  the same button in the day's last two minutes.
- **The screens that end or pause the time** end with "Şimdi ne yapsak?": one card -
  a big picture and one sentence - or, in a break, "Haydi biraz hareket!" and three
  (D-231). Zio reads them after the daemon's own line.
- **Game tiles waiting for the day's learning** are quieter and carry "🔒 10 dk
  öğrenince" (D-232); a tap gets the calm screen with a bar that fills and one button,
  "📚 Öğrenme uygulamaları". No ask button: it is a rule about order.
- **A parent's "well done" or note** (D-233) comes over whatever is on the screen: Zio
  happy, a star or an envelope, "Anne sana aferin dedi!" or "Anne yazdı:" and the
  sentence in a bubble, one "Tamam". It goes by itself after a quarter of a minute.
- **"👨‍👧 Birlikteyiz · süre sayılmıyor"** under the ring while a parent is with the child
  (D-235); **"🎂 İyi ki doğdun Elif!"** on the birthday (D-236), said once that day.
- **A calm screen** (D-236): nothing moves, pulses or celebrates; the same screens and
  the same words.
- **Sesim** (D-240): one big round coral button, "Basılı tut ve konuş", that turns red,
  grows a little and counts the seconds while it listens; the recordings under it, each
  with ▶, "💌 Büyüğüme gönder" and a bin that asks once (tap again within three seconds).
- **Okuma** (D-241): the door is the five sound groups as a path, the met letters lit,
  and "▶ Bugünün dersi". A lesson is one card at a time: the letter as big as the screen
  allows, capital and small; or the instruction ("Dinle. Hangi harfle başlıyor?", "Oku ve
  resmini bul.") with a 🔊 button and three big choices. Right is green and a star; wrong
  is a shake and the sound once more.

## 3. Parent panel

### 3.1 Information architecture

The menu has six entries (`templates/base.html`), which still fit the phone's
bottom bar at 360 px. Above 40rem they are a row at the top, tightened just above
the breakpoint so that the English labels fit at 640 px; with a larger font the row
wraps to a second line rather than letting the page scroll sideways:

```
Bugün / Today          ← default, the only screen most parents open; a badge
                         counts pending requests, and the banner opens İstekler
Çocuklar / Children    → per child: Süre kuralları (time, and special days, D-215),
                         Uygulamalar ve siteler (D-164), İnternet (D-167), Asistan
                         (D-165), Düzenle (D-191)
Geçmiş / History       → fourteen days of minutes, and one day's apps, sites and
                         requests (D-192)
Analiz / Insights      → a week or a month in plain words, then the numbers (D-206)
Kayıtlar / Audit log
Ayarlar / Settings     → language, Görünüm (text size and contrast, the signed-in
                         parent's own, D-217), Girişte panel (D-227), email, Zio and Sio's voice: the
                         natural voice to download (D-196) and the parent's own
                         (D-174), phone access, Ebeveynler (D-218), your PIN, the
                         family's passphrase
```

**Ebeveynler** (`/settings/parents`, D-218) lists everyone who can sign in, with
"(siz)" beside the signed-in parent, and lets them change their own name. Under
**Ebeveyn ekle**, closed by default: the new parent's name, their own PIN twice, their
recovery code shown once with "Yeni ebeveyn bu kodu bir yere yazdı", and the
passphrase. Each other parent has **Kaldır**, red, behind the passphrase, saying that
they will no longer sign in and that what they did stays in Kayıtlar under their name.
One line says what stays shared: the passphrase, the mail settings and the children's
requests. With two or more parents the sign-in page first asks **"Kim giriş
yapıyor?"**, a big button per name, then that parent's PIN with "Başka biri
misiniz?"; with one, it is the PIN box it always was. Kayıtlar names the parent on
each row, and İstekler says who answered.

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
exists keeps the tiles their parent chose, so a new app reaches them only when it is
ticked. The one exception is a tile that takes over from another (D-203's `inherits`):
the upgrade puts it beside the old one, once, for each child who had the old one —
Marble beside Google Earth — and the audit log says so. For thirty days after the
upgrade each new tile carries a small **Yeni**, and one line at the top names those
that suit the child's age, can start on this machine and are not ticked yet, and says
that they are not on the child's screen and how to add them: "Bu çocuğun yaşına uygun
2 yeni uygulama henüz ekranında yok: Patates Kafa, Hafıza. Eklemek için işaretleyin."
The same line, without its last sentence and as a link to the screen, sits under the
child on **Çocuklar**; it is worded so that a parent does not read it as news of
something the child already has. Saving that child's screen after the upgrade answers
it: a parent who looked and left them unticked is not asked again, though the **Yeni**
stays. A save from before the upgrade, or another child's, does not count. A fresh
install marks nothing new.

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

**Kapat** asks first, then says "Bilgisayar kapatılıyor." at once; the computer goes a
few seconds later, after the children's apps have closed (D-209). If it does not go,
every child's screen is locked and the parent gets an alert.

Pending access requests appear as a banner above the cards, because they are
time-sensitive — a child is standing at the computer waiting.

**Special days** (D-215, §3.10). Under the buttons, closed, **🎉 Bugün özel bir gün
mü?** opens three choices for today only - **+1 sa**, **+2 sa**, **Süre sınırı
yok** (the card's own **+30 dk** is above) - a line saying tomorrow is an ordinary day again and a second choice
replaces the first, and a link to the time screen for several days. A day that is
special shows on the card as "🎉 Bugün özel gün: +1 sa" (or "Tatil (20.01 – 31.01):
hafta sonu kuralları") with **Kaldır**, which asks first. Read from the database, so
the line is there with the daemon down. A tap after the time ran out lets the child
back in, as **+10 dk** does.

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
2. Parent account and PIN: the first parent; any other is added later under
   **Ayarlar → Ebeveynler** (D-218)
3. Passphrase and the 8-word recovery code (shown once, with a "I wrote it down" check);
   the passphrase becomes the family's
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
9. Extras: Zio and Sio's voices, on by default, and their natural voice (about
   90 MB, downloaded from its authors), ticked for a Turkish family (D-223). The
   assistant is switched on per child under **Çocuklar → Asistan** (D-165).

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
logo. The parent's own account is shown by the name they gave the wizard, with a
house (D-221), not by its user name beside a grey silhouette; a name or a picture the
parent chose themselves is kept.

**Yardım / Help** (`/help`) is in the top bar and on the sign-in page: the questions
parents ask first, each answered in the words the screens use. It is open before
signing in, because "PIN'imi unuttum" is one of them. It starts with **Adım adım
rehberler** (D-224): mail, phone access, adding a child, time, the internet, the
voices, a forgotten PIN or passphrase - each at `/guides/<slug>`, every step numbered,
with the commands to type and a "Sorun mu var?" section. The mail card links to its
guide, and a failed test mail says whether the password was refused or the server not
reached.

### 3.5 PWA

`manifest.json`, maskable icon, `display: standalone`, a service worker caching the
shell so the app opens instantly. Never cache live data: with the tailnet unreachable
the parent sees `/offline`, one sentence saying the panel could not be reached,
rendered without the session so the cached copy holds no name and no token (D-194).

### 3.6 A child's details

**Çocuklar → Düzenle** (`/children/{id}/edit`, D-191): name, birth year, picture and
language; under **Ekran** (D-217), folded away until something is chosen, the text
size (Normal, Büyük, Çok büyük), high contrast, and the reading font, OpenDyslexic,
with a line saying so when it is not installed on this computer; and under
**Gelişmiş** the supervision indicator — shown, a small icon, or
hidden (the first-run explanation still runs, D-76). The account name is shown, not
editable. Saving a new name or picture updates the login screen; saving the screen
choices redraws the launcher.

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
more than before, where does the time go, is there anything I should do? Searches the
browser refused (D-222) come first after the highlights, as **Engellenen aramalar**:
each one's words and time, a few sentences on talking about it calmly, and "Gördüm",
which clears the banner Today shows for them. One child at a
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
- Stickers in the child's album (§2.8, D-211) are good news from two: "Albümüne 3
  çıkartma ekledi: öğrenme uygulamalarında en az 15 dakika geçen her gün için bir
  tane." Low in the order, above the busiest hours.
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

### 3.10 Special days

**Süre kuralları** ends with a card of its own, **🎉 Özel günler** (D-215), outside
the rules' form because a special day is an exception to them and takes effect
without saving them. One line says what it is for and that the limits per kind of
app still apply. Then:

- today's and the coming special days, one sentence each ("Bugün özel gün: +1 sa",
  "Tatil (20.01 – 31.01): hafta sonu kuralları", "10.11: günde 3 sa 20 dk") with
  **Kaldır**; or "Şu an özel gün yok.";
- **Bugün özel bir gün mü?** and the same four choices as on Today;
- closed, **Birkaç gün için: tatil, bayram**: the first and the last day (the
  phone's date pickers, from today), a name if the parent wants one ("Tatil"), and
  **Hafta sonu gibi** (the weekend's time and hours, the default) or **Günde şu
  kadar dakika**. One line says both days are included and that days already over
  do not change.

A start in the past is moved to today; an end in the past, an end before the start
or more than 100 days are refused with a sentence that says so. **Kaldır** on a
holiday already under way ends it today, and the days before keep it: **Geçmiş**
draws them against the quota they had.

---

### 3.11 The English corner

**İngilizce** (`/english` on the launcher, D-225) is a tile like Resimlerim: a page of
the launcher with the word of the day (tap to hear), one big "Bugünün dersi" button
(or "Biraz daha oyna" once it is done), "Sözlüğüm" and the twelve units as a path, done
ones ticked and the current one ringed. A lesson is one screen per step - meet, find,
say back, story - with a bar and stars at the top and "İngilizce köşesi" to leave. Every
word is said aloud before anything is asked, a wrong tap shakes and says the word again,
and the buttons wait while a voice speaks. The parent sees it in **Analiz**, as a card
and a highlight; there is no setting beyond the tile itself.

Where the assistant is on, the corner also has **Sio ile İngilizce konuş** (D-226): the
assistant's page with Sio on the left, speaking first in English, the child's words to
tap under the box, and the same note that a parent may see what is said.

### 3.12 What the family touches added to the panel (D-230 to D-239)

- **Today's card** gains, each only when it applies: "🏁 son turunu kullandı", "💛
  Aferininizi gördü" or "Notunuz bekliyor", "👨‍👧 Birlikte modu açık" with Bitir, "🎂
  Bugün doğum günü", and a child's recording with a player. Under the buttons, closed like
  the special day: "👨‍👧 Birlikteyiz" (30 dk, 1 sa, 2 sa) and "💛 Bir şey gönder" (Aferin!,
  or a note). The card still reads in three seconds: everything new is either a line
  that is there only when something happened, or folded away.
- **Bugün ne konuşalım?** (D-234) is a card of its own under the children, above the
  week, for the evening.
- **The time screen** has quick choices under each quota and, under Gelişmiş, the last
  round (Kapalı, 2, 3, 5 dakika) and the learning-first minutes, each with its one line.
- **Çocuklar** has "👀 Ekranını gör" for each child (D-239); **Düzenle** has the
  birthday (two selects, both or neither) and, under Ekran, "Sakin ekran".
- **Ayarlar** opens with **Koruma durumu** (D-237) and links to **Yedekle ve taşı**
  (D-238); Today shows one line when something on the protection list needs the parent.
- **Analiz** has a reading card beside the English one (D-241).

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
- Text size in three steps, Normal, Büyük and Çok büyük (100, 125 and 150 per cent),
  and high contrast: for each parent under **Ayarlar → Görünüm**, kept with their
  account so it follows them to the phone, and for each child under **Çocuklar →
  Düzenle → Ekran** (D-217). Three attributes on `<html>` (`data-text-size`,
  `data-contrast`, `data-font`) that `tokens.css` turns into token overrides. In high
  contrast, words reach 7:1 and lines, rings and borders 3:1 (`tests/test_appearance.py`).
- A reading font for a child, OpenDyslexic, from Debian's `fonts-opendyslexic`, which
  biokids-desktop recommends. Not vendored: the browser finds it among the machine's
  fonts, and without it the usual font stays.
- Colour is never the only signal: the time ring also changes thickness, and warning
  banners carry an icon.
