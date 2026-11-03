# Undercards Deck Printer

A **local, fan-made** tool that turns an [Undercards](https://undercards.net)
deck code into **print-ready PDF sheets**: every card comes out at exactly
**2.5 × 3.5 in (63.5 × 88.9 mm)** — real poker-card size, no matter how big
the deck is. Bigger decks mean **more pages, never smaller cards**.

> This is an unofficial hobby tool. Not affiliated with or endorsed by
> Undercards. Card art, names and descriptions belong to their respective
> authors; this tool only prints what you paste for **personal use** — it
> never publishes, uploads or shares anything.

---

## Quick start (Windows)

```bat
:: one-time preparation
python -m pip install -r requirements.txt

:: start (first run builds the local card database, ~1 minute)
run.bat
```

Your browser opens at `http://127.0.0.1:8952`. Paste a deck code, press
**Print preview**, check the missing-art list, then **Download deck PDF**.
Anything else is available manually:

```bat
python tools\build_db.py          :: (re)build data\db.json + data\en.json
python -m app.webapp              :: start the server (no auto db build)
python -m pytest                  :: run the test suite
python tools\ui_smoke.py          :: optional headless-Chrome UI smoke test (needs playwright)
python tools\build_db.py --no-network   :: offline rebuild from the bundled snapshot
```

Other ports: `set UC_PORT=9001` before starting.

---

## Deploying this folder — "nonlocal"

> **This folder is a standalone, self-contained copy** of the tool prepared
> for hosting. The copy in the repo root stays **exactly** as it is
> (localhost-only, no gate). Nothing in the root tree is modified by a
> deployment; this folder is the only thing you push to a host.

What is different here, and why:

| Thing | Root copy | This copy |
|---|---|---|
| Access | always open on `127.0.0.1` | **public by default**; optional `UC_ACCESS_TOKEN` lock-in (Basic username/password or `Bearer`) |
| Bind | hard-wired `127.0.0.1` | `UC_BIND=0.0.0.0` so the host's router can reach it |
| Port | `UC_PORT` (default 8952) | `PORT` (set by the host) → `UC_PORT` → 8952 |
| Harvest | optional Playwright window | **disabled** (no Playwright in requirements; `/api/harvest/start` replies 400 cleanly) |
| Disk | persistent | ephemeral: brand-new hosts start with the bundled `data/db.json` / `data/en.json`; the card cache, UI-refresh results and job folders reset on every restart |

Anyone with the deployed URL can use it, no login prompt. If you ever want
it private again, add one env var in the host's dashboard:

```
UC_ACCESS_TOKEN = <your secret string>
```

and every request must then present that token (Basic username — or
password — or `Authorization: Bearer <token>`); `/api/health` stays open for
the host's liveness probe. The value is re-read per request, so removing it
takes effect on the next deploy/restart, and it never has to live in git.

### Step-by-step (Render.com free tier; your account, your click)

1. Create a **private** GitHub repository, e.g. `undercards-deck-printer`.
2. Push **only this folder** to it (from this folder's directory):
   ```powershell
   git remote add origin https://github.com/<you>/undercards-deck-printer.git
   git push -u origin main
   ```
3. On <https://render.com> → **New → Blueprint** → pick the repo. Render reads
   `render.yaml` (service, build/start commands, health check at
   `/api/health`) and just asks you to confirm — there is no token step.
4. **Create.** First build takes a few minutes; afterwards the URL is
   `https://<service>.onrender.com`, open to anyone you share it with.
5. Checking it works:
   ```powershell
   curl.exe https://<service>.onrender.com/api/health
   ```

Free-tier caveats (honest, documented): the service sleeps after ~15 minutes
idle (first visitor waits ~30–60 s — a free uptime monitor pinging
`/api/health` every 10 min keeps it warm), the disk is **ephemeral** as
listed above, and no Playwright means the sign-in harvest button will
politely refuse on the hosted copy. Nothing is ever uploaded anywhere else;
the PDF is still generated on the spot and served only to the requester.

*Nothing is published by this repo itself — steps 1–5 are yours to perform;
until then the only working copy remains this machine's localhost tool.*

---

## The deck code format (verified)

Verified against the official undercards.net client and export tools
(currency of this verification: late 2025 / 2026 seasons):

* the code is **base64 of a UTF-8 JSON object**:
  ```json
  {"soul": "JUSTICE", "cardIds": [258, 258, 258, ...], "artifactIds": [3, 20]}
  ```
* **duplicates are quantities**: printing `258` three times in `cardIds`
  means three copies. The tool preserves duplicates and original order
  exactly (they also define stack order in print).
* `artifacts` control in-game passives — they are **not printed by default**.
* shimmy’s shiny variants are **never encoded** in the code; the printed
  face is therefore always the normal (non-shiny) art.
* the whole share URL (`https://undercards.net/Decks/?code=...`) is accepted
  as-is, as is the `#fragment` form; aliases (`cards`, `artifacts`,
  `shinyIds`) and a few legacy shapes are tolerated.
* **nothing is ever guessed**: an unparseable or unknown payload raises a
  precise `DeckParseError` shown verbatim in the UI.

---

## Data sources — what comes from where (honesty section)

| Data | Source | Status |
|---|---|---|
| Card **names & descriptions** (coloured stats / keywords) | `data/en.json` — the official public translation file the game client loads (`/translation/en.json`) | live fetch each **Refresh**, verbatim snapshot bundled (**2nd Nov 2026**, 3469 keys) so the app stays offline-capable |
| Rendering of `{{HP:3}}`, `{{STATS}}`, `{{CARD:15}}`, `{{PLURAL}}` … | re-implemented 1:1 from the game's own files and verified by running the official i18n stack under node | see “i18n ground truth” below |
| Card **metadata** (cost/atk/hp/soul/type/extension/… ) | `data/db.json` — parsed from every `{{Infobox Monster/Spell/Artifact}}` on [undercards.miraheze.org](https://undercards.miraheze.org) (CC BY-SA) | fallback by design; the live game's `/AllCards` dump is authoritative but requires a signed-in session (next row) |
| The **authoritative** metadata (`/AllCards`) | optional **harvest**: a visible browser window you log into yourself; the app then performs the very same `GET /AllCards` request the page performs, with your session's own cookies | **Playwright optional dependency.** We do **not** bypass Cloudflare/login/rate limits under any circumstances. If you do not run the harvest the app still works — wiki fields are usually up to date for all current-card usages |
| Card **art**, frames, fonts, rarity gems | public statics from undercards.net, politely throttled (≥ 0.3 s apart) and cached on disk; wiki-hosted mirror as second source | missing art is never invented — a clearly striped **MISSING ART** placeholder is composed instead |
| `db.json` build provenance | stored inside `db.json → _meta` (timestamps, page counts, skipped pages) | visible via `GET /api/dbinfo` |

CC BY-SA note: the wiki-derived metadata and any wiki-hosted file copies are
licensed **CC BY-SA 4.0** by the Undercards wiki authors. If you share output
beyond personal use, keep that attribution requirement in mind.

---

## Printing — the hard invariants

* every face is **exactly 180 × 252 pt** on the page (`2.5 × 3.5 in`):
  asserted by tests *reading the PDF content streams back* (the `180 0 0 252
  cm` placement matrix is checked per card);
* grids: **landscape 4 × 2 = 8 cards/page**, **portrait 3 × 2 = 6** — centred
  with **equal margins on every side**, optionally with a small gap you can
  set (0–10 mm);
* option **cutting guides** = dashed cut lines exactly on each card edge plus
  classic offset **corner crop marks**;
* sheets rasterize at **~300 DPI** (faces are composed at 880 × 1230 px);
* Paper: **Letter** (default) or **A4** — in landscape both fit the same
  4 × 2 grid, so the card size truly never changes between paper sizes;
* preview thumbnails in the UI are rasters of the very same layout the PDF
  uses.

### Missing art is a decision, not a surprise

If any card's art cannot be fetched from either source:

1. the pipeline still completes so you can see the **placeholder** cards,
2. the **deck PDF refuses to download** (HTTP 409) until you press
   **“Print anyway”**, and the honest placeholder faces are what ships then.

---

## Optional `/AllCards` harvest — how it works and what it does NOT do

* starts a **visible** Chromium (Playwright, persistent profile, your own
  cookies) — you browse and sign in *yourself*;
* once inside, the app fetches `/AllCards` **from inside the page** with
  plain `fetch(...)`, exactly what the game client does on every page load;
* the payload replaces wiki rows in `data/db.json` (fields it owns: image
  names, frame skins, stats, tribes…) and shows `authoritative` in the
  header chip;
* **no credential handling by the app, no headless evasion, no CAPTCHA or
  challenge bypassing, no rate-limit tricks**; nothing leaves your machine;
* the raw dump is kept at `data/allcards_raw.json` for your own inspection.

Install the optional dependency only if you want this:

```bat
python -m pip install playwright
python -m playwright install chromium
```

---

## Known approximations (by design, documented)

* **Artifact sheet** — the official client has no printable artifact “card”,
  so the optional sheet uses a clearly-labelled simplified face (name +
  description + emblem, Undertale-frame proportions). Main deck cards are
  unaffected.
* **Tribes icons** are intentionally not drawn (the game has no print path
  for them); tribe names still appear in descriptions where the official
  emitters include them.
* The UI chrome (headings, buttons, labels) uses the game's own
  **Determination Mono** web font, served from the local font cache via
  `/api/font/DTM-Mono.otf`. Until a first card job fetches it the UI simply
  falls back to a plain monospace.
* Wiki fallback stats can lag behind a fresh game patch until you press
  **Refresh card database** (or run a harvest).
* If `fonts/DTM-Mono.otf` cannot be fetched yet, text-heavy faces are listed
  in the missing table with a retry message; all other font needs are
  provided by the site's own files.
* Shiny frame variants exist in-game; since the deck code cannot encode
  them, normal faces are always printed.

---

## Project layout

```
app/
  deck_parser.py   base64 deck code -> Deck (duplicates resolved, order kept)
  i18n.py          official-{{...}} renderer (stats/PLURAL/CARD/KW/... emitters)
  card_db.py       in-memory DB + i18n + /AllCards merge + persistence
  wiki_fallback.py polite wiki crawler + {{Infobox ...}} parser
  assets.py        rate-limited downloader + disk cache (site / wiki / fonts)
  cardface.py      Pillow compositor (official 176x246 geometry, integer scale)
  pdfgen.py        fixed-size sheets, centred grids, crop marks, raster preview
  harvest.py       visible-browser /AllCards harvest (Playwright, optional)
  webapp.py        Flask server + job pipeline + all /api endpoints
  templates/ static/   minimal dark UI (vanilla JS, no build step)
tools/build_db.py  builds data\db.json + data\en.json (+ snapshot fallback)
tools/ui_smoke.py  optional headless-Chrome UI smoke test against a running server
data/              db.json, en.json, en_official_snapshot.json, jobs/, cache/
tests/             parser / i18n / compositor / pdf invariants / webapp flows
run.bat            double-click launcher (Windows)
```

### HTTP API (all JSON, localhost only)

| Endpoint | Purpose |
|---|---|
| `POST /api/parse` | `{code, settings}` → job id; decoding errors surface when polling |
| `GET /api/job/<id>` | full snapshot: status, stage, art progress, entries, missing, counts |
| `POST /api/settings/<id>` | change paper/orientation/gap/guides/artifacts/proceed_missing |
| `GET /api/preview/<id>?page=N&which=deck\|artifacts` | PNG raster of a sheet |
| `GET /api/face/<id>?k=K` | composed card face PNG (UI thumbnails) |
| `GET /api/font/<name>` | serve the cached site fonts (`DTM-Mono.otf`, `sans.ttf`) for the UI itself |
| `GET /api/download/<id>?which=deck\|artifacts` | the PDF (409 + reasons while art missing) |
| `GET /api/dbinfo`, `POST /api/db/refresh` | DB provenance / polite web rebuild |
| `POST /api/harvest/start\|cancel`, `GET /api/harvest/status` | optional sign-in harvest |
| `GET /api/health` | quick readiness probe |

---

## “i18n ground truth”

The message renderer in `app/i18n.py` was built by executing the **official**
jquery.i18n parser/emitter chain with the game's own `translation.js` and live
`en.json` under node, and matching outputs test-by-test. Semantics locked by
the suite include: `$1` substitution ordering, innermost-first emitters,
stat words coloured but numbers plain (uncoloured “3 ” then coloured “HP”),
`{{STATS}}` node-color mapping (2 nodes → atk/hp; 3 → cost/atk/hp) with
uncoloured signs, `{{CARD}}` cyan re-render with PLURAL, white+underlined
`{{KW}}/{{ENCHANT}}/{{ARTIFACT}}`, `override=` patterns, and the raw-key
fallback for unknown/missing keys.

---

## Troubleshooting

* *“Card database is missing”* → press **Refresh card database** or run
  `python tools\build_db.py` once.
* Artifacts/Cloudflare warnings in the log → retry later; nothing needs a
  bypass, ever.
* The harvest button says Playwright is missing → the two `pip install
  playwright` lines in section above.
* Port busy → `set UC_PORT=9001` then start again.

## Tests

```bat
python -m pytest
```

covers: deck-code parsing (duplicates/order/aliases/errors), i18n fidelity
fixtures, compositor geometry & placeholder art, PDF **fixed-size +
pagination invariants** (content-stream level), and full offline web-pipeline
flows including the missing-art block & proceed-anyway path.

On top of that, `tools\ui_smoke.py` walks the real UI in a headless Chrome
(load → print flow → quantities → sheet previews → settings re-render →
error paths → zero console errors) once you have Playwright installed; it
needs the dev server running.
