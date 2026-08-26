# Print Bridge

![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
![Node 18+](https://img.shields.io/badge/node-18%2B-339933.svg)

A small Windows-only background service that lets a webpage print silently to
ESC/POS thermal/receipt/label printers - no browser print dialog, no QZ Tray.

A webpage calls `http://localhost:9187/...`; Print Bridge turns that into a
raw ESC/POS job and sends it straight to the Windows print spooler via
`win32print`/`python-escpos`. Its own config UI (Printers / Logs / Settings)
is a small React app served by the same process at `http://localhost:9187/`.

Not supported (by design): anything that isn't ESC/POS (ZPL/EPL/etc.), macOS/
Linux, USB/serial/HID device access, WebSockets, cloud sync.

## Features

- **Simple HTTP API** for printing - JSON in, receipt out. No browser print
  dialog, no printer drivers to fight with from the calling webpage's side.
- **Structured text printing** (`/print/text`) - line items, bold/scaled
  text, barcodes, QR codes, cash-drawer kick, multiple copies.
- **PDF printing** (`/print/pdf`) - upload any PDF and it's rasterized and
  printed page by page, logos/tables/mixed scripts and all.
- **Correct Arabic/RTL rendering everywhere** - not just the dedicated test
  print: any Arabic line sent to `/print/text` is detected automatically and
  shaped/reordered correctly, so an English/Arabic-mixed receipt prints both
  scripts right, line by line.
- **Dry-run previews** - `/print/text` and `/print/pdf` both accept
  `dry_run: true` and return a rendered preview image instead of printing,
  so you can check a receipt or PDF before committing paper to it.
- **Windows or network printers** - map a logical name to an installed
  Windows printer, or to a raw ESC/POS printer reachable directly over TCP
  (no Windows driver needed at all).
- **Live status + automatic retry** - each mapping shows a status indicator
  (spooler status for Windows printers, reachability for network ones), and
  a transient failure (spooler busy, printer briefly offline) is retried
  with backoff before giving up, visibly logged either way.
- **Failover targets** - a logical printer can have an ordered list of
  targets (a primary plus one or more backups, Windows or network, any
  mix); if the primary exhausts its own retries, the job falls through to
  the next target automatically, and the log shows which one actually
  served it.
- **Rate limiting** - a configurable cap on `/print/*` requests per minute
  (keyed by auth token or caller origin), so a runaway integration can't
  flood the spooler/printer - `429` with `Retry-After` past the limit.
- **A real config UI** (dark-mode React app) for mapping logical printer
  names to real printers, watching print-job history, and managing server
  settings - no hand-editing JSON required.
- **System tray app** with a packaged single-file `.exe` - no Python/Node
  needed on the machine that runs it.
- **Locked down by default** - binds to `127.0.0.1` only, explicit CORS
  origin allow-list (no wildcards), optional shared-secret auth token, and
  correct handling of Chrome's Private Network Access preflight.

## Table of contents

- [Running it](#running-it)
- [First-time setup](#first-time-setup)
- [HTTP API](#http-api)
- [Which format should I use?](#which-format-should-i-use)
- [CORS and Chrome's Local Network Access prompt](#cors-and-chromes-local-network-access-prompt)
- ["Start with Windows"](#start-with-windows)
- [Architecture notes](#architecture-notes)
- [Building](#building)
- [Testing it end-to-end](#testing-it-end-to-end)
- [Project layout](#project-layout)
- [License](#license)

## Running it

**From source** (Python 3.11+, Node.js 18+ for the frontend build):

```
git clone https://github.com/dilshad-khalil/Receipt-Bridge.git
cd Receipt-Bridge

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

cd frontend
npm install
npm run build
cd ..

.venv\Scripts\python run.py
```

The frontend build (`frontend/dist/`) is not checked into source control -
`python run.py` needs it to already exist (it serves a "frontend hasn't been
built yet" page with instructions if it's missing), so the `npm run build`
step above is required once per checkout, not just when packaging the .exe.

A tray icon appears (bottom-right, may be under the "^" overflow arrow). The
config UI is at `http://localhost:9187/` (or whatever port you've configured).

**As a built .exe**: see [Building](#building) below, then just run
`dist\PrintBridge.exe`. It has no console window; check the tray icon's
"View logs" for what's happening.

First run creates `%APPDATA%\PrintBridge\config.json` with defaults (port
9187, no allowed origins, no auth token, no printer mappings) and starts
logging to `%APPDATA%\PrintBridge\logs\print_bridge.log` (rotates at ~1MB,
5 backups kept) plus a small `%APPDATA%\PrintBridge\logs\jobs.db` SQLite
file holding the last ~200 print attempts (see [`GET /logs`](#get-logs)).
Config lives there regardless of where the .exe is installed, so it
survives reinstalls/updates and never needs write access to something like
`C:\Program Files`.

## First-time setup

1. Open `http://localhost:9187/` (the tray icon's "Open printer setup" does
   this for you). This is the **Printers** page.
2. Pick a target - **Windows printer** (choose one from the dropdown) or
   **Network printer** (enter its IP and ESC/POS port, usually `9100` - no
   Windows driver needed) - give it a logical name (e.g. `receipt_printer_1`),
   click "Save mapping". Your webpage will refer to printers by this logical
   name, never the real Windows printer name or IP - that indirection is
   what lets you repoint `receipt_printer_1` at a different physical printer
   on a different till without changing any app code. DPI/width are optional
   and only matter for PDF printing - see [`POST /print/pdf`](#post-printpdf).
3. Open the row's actions menu (the "..." button) and use "Test print" /
   "Print PDF" to confirm it actually prints (see
   [Testing it end-to-end](#testing-it-end-to-end)). The status badge next
   to it updates every ~15s - "Ready"/"Reachable" means the printer
   answered; anything else (offline, paper out, unreachable, ...) is worth
   checking before you rely on it. The same menu's "Manage targets" is
   where you add backup targets for failover (see
   [Architecture notes](#architecture-notes)).
4. Switch to the **Settings** page, add your website's exact origin (e.g.
   `https://ourapp.example.com`) to "Allowed origins" and save. **Restart**
   Print Bridge (tray icon -> "Restart server") for the port/origins change
   to take effect - both are only read once at startup.
5. Check the **Logs** page any time something doesn't print - every attempt
   (success or failure, with the error message) is recorded there.

## HTTP API

All endpoints are on `127.0.0.1` only (never reachable from the network).

| Method | Path | Auth? | Purpose |
|---|---|---|---|
| GET | `/health` | no | `{"status":"ok","version":"..."}` - liveness check |
| GET | `/printers` | no | Windows printers installed on this machine |
| GET | `/config` | if token set | current port/origins/printer mappings (each with a live status)/startup state |
| POST | `/config/printers` | if token set | add a mapping, or replace an existing one's entire target list with a single target |
| POST | `/config/printers/{logical_name}/targets` | if token set | replace an existing mapping's ordered failover target list |
| GET | `/config/printers/{logical_name}/status` | if token set | live status for one mapping's primary target |
| DELETE | `/config/printers/{logical_name}` | if token set | remove a mapping (idempotent) |
| POST | `/config/settings` | if token set | update port/allowed_origins/auth_token/rate_limit_per_minute (port/origins need a restart) |
| POST | `/config/startup` | if token set | enable/disable "Start with Windows" (immediate) |
| POST | `/print/text` | if token set | render+print structured content (the main endpoint) |
| POST | `/print/raw` | if token set | send caller-built raw ESC/POS bytes (base64) |
| POST | `/print/test` | if token set | combined connectivity check + bilingual English/Arabic test print |
| POST | `/print/pdf` | if token set | rasterize and print an uploaded PDF, page by page |
| GET | `/logs` | if token set | recent print-job history (success/fail + error) |
| GET | `/` | no | the config UI (Printers / Logs / Settings) |

### `POST /print/text`

```json
{
  "printer": "receipt_printer_1",
  "lines": [
    { "text": "ACME STORE", "align": "center", "bold": true, "width": 2, "height": 2 },
    { "text": "------------------------", "align": "center" },
    { "text": "1x Espresso        $3.50", "align": "left" },
    { "text": "شكرا لزيارتكم", "align": "center" }
  ],
  "barcode": { "type": "code128", "data": "0123456789" },
  "qr": { "data": "https://example.com/order/123" },
  "cut": "full",
  "open_drawer": false,
  "copies": 1,
  "dry_run": false
}
```

`printer` is the **logical name** from step 2 above, resolved server-side to
the real Windows printer or network printer it's mapped to. `align` is
`left`/`center`/`right`; `width`/`height` are text scale 1-8; `barcode`,
`qr`, `open_drawer` are all optional. `barcode.type` accepts the usual
ESC/POS symbologies (`CODE128`, `CODE39`, `EAN13`, `EAN8`, `UPC-A`, `ITF`,
... - case-insensitive). `cut` is `"none"` (default), `"partial"`, or
`"full"` - the original boolean is still accepted for existing integrations
(`true` -> `"full"`, `false` -> `"none"`).

Any line containing Arabic is detected automatically and rendered as a
bitmap (shaped + bidi-reordered, same pipeline as `/print/test` - see
[Architecture notes](#architecture-notes)) instead of native ESC/POS text,
so a receipt mixing English and Arabic lines - like the example above -
prints both correctly. Pure-Latin lines keep using the fast native text
path.

`dry_run: true` skips the printer entirely and returns a rendered preview
instead: `{"ok": true, "dry_run": true, "preview_images_base64": ["..."]}`
- one base64 PNG approximating the job's layout (align/bold/width/height,
Arabic included), good for checking a receipt before committing paper to
it. Not written to the job log, since nothing was actually printed.

Responses (non-dry-run): `{"ok": true, "job_id": "..."}` on success; `404`
if the logical printer name isn't configured; `429` (with a `Retry-After`
header) if you've exceeded the configured rate limit (see
[Rate limiting](#rate-limiting)); `502` (with the underlying error as the
detail) if every configured target rejected the job after each one's own
retries were exhausted (see [Architecture notes](#architecture-notes)).
Every attempt (success or failure) is written to both the log file and
`GET /logs`, including how many attempts it took and which target actually
served it.

### `POST /print/raw`

```json
{ "printer": "receipt_printer_1", "data_base64": "GxAAG0AA..." }
```

Escape hatch for callers who build their own ESC/POS bytes - decoded and
written straight to the printer.

### `POST /print/test`

```json
{ "printer": "receipt_printer_1", "width_px": 384, "cut": true }
```

The single "Test print" action - one combined job containing a connectivity
header ("If you can read this, printing from ... works"), a horizontal
rule, the bilingual EN/AR test quotes (one English quote, its Arabic
counterpart, each with a divider), and a QR code. It's the "Test print"
button in the config UI's Printers page; besides confirming a mapping
prints at all, it's also the best single check that non-Latin text
actually comes out right on a given printer, which is the part most likely
to silently break in ways the rest of the API can't detect.

`width_px` should roughly match the printer's dot width - `384` for a 58mm
printer (the default), `576` for 80mm.

**Why the header+quotes are one image, not `lines` text:** see
[Architecture notes](#architecture-notes) below.

### `POST /print/pdf`

`multipart/form-data`, not JSON/base64 - PDFs can be non-trivial in size and
multipart avoids ~33% base64 inflation, and is what the browser's
`FormData`/`fetch` produces natively for a file input.

| Field | Required | Meaning |
|---|---|---|
| `file` | yes | the PDF |
| `printer` | yes | logical printer name |
| `dpi` | no | override the printer's configured DPI for this job only |
| `cut_between_pages` | no (default `true`) | cut between pages; the last page is always cut |
| `dry_run` | no (default `false`) | rasterize and return preview images without printing |

```js
const form = new FormData();
form.append("printer", "receipt_printer_1");
form.append("file", pdfFile); // a File/Blob, e.g. from <input type="file">
form.append("cut_between_pages", "true");
await fetch("http://localhost:9187/print/pdf", { method: "POST", body: form });
```

Each page is rasterized at the printer's configured DPI (per-mapping
`dpi`/`width_px`, set on the Printers page - defaults are 203 DPI, 384px
width) and dithered to 1-bit (Floyd-Steinberg), then sent through the same
ESC/POS raster-image code path as the EN/AR test quote (see
[Architecture notes](#architecture-notes)). Response:
`{"ok": true, "job_id": "...", "pages_printed": N}`.

`dry_run: true` skips the print step - rasterization already happens
unconditionally, so this is nearly free - and instead returns
`{"ok": true, "dry_run": true, "preview_images_base64": ["...", ...]}`, one
base64 PNG per page, the printer never touched. Not written to the job log.

### `GET /logs`

```json
{ "jobs": [
  { "timestamp": 1712345678.9, "endpoint": "print/pdf", "printer": "receipt_printer_1",
    "ok": true, "error": null, "job_id": "...", "attempts": 1, "served_by": "EPSON TM-T20III Receipt" },
  { "timestamp": 1712345670.1, "endpoint": "print/text", "printer": "till_2",
    "ok": true, "error": null, "job_id": "...", "attempts": 5, "served_by": "192.168.1.51:9100" }
]}
```

The last ~200 attempts across every `/print/*` endpoint, newest first,
backed by a small SQLite file (`%APPDATA%\PrintBridge\logs\jobs.db`) so it
survives a restart. `attempts` is 1 unless a transient failure (spooler
busy, printer briefly offline/unreachable) was retried before the job's
final outcome - see [Architecture notes](#architecture-notes). `served_by`
names whichever configured target actually completed the job - useful on
a mapping with failover backups, where it may differ from the primary
target; `null` if the job never reached a target or every target failed.
Optional `?limit=N` query param. Powers the Logs page.

### `POST /config/printers`

```json
{ "logical_name": "receipt_printer_1", "type": "windows", "windows_printer_name": "EPSON TM-T20III Receipt" }
```
```json
{ "logical_name": "kitchen_printer", "type": "network", "host": "192.168.1.50", "port": 9100 }
```

Adds a mapping (a single target), or replaces an existing mapping's
**entire target list** with just this one target - the simple path used by
the Printers page's "Add mapping" form. A `"windows"` mapping is rejected
with `400` up front if that printer name isn't currently installed/visible
(`GET /printers` lists what's available); a `"network"` mapping can't be
validated that way - it's accepted as given, and its status (see below) is
how you find out if it's actually reachable. `dpi`/`width_px` are optional
on every call - omit them to leave an existing mapping's values untouched,
or to use the defaults (203 DPI, 384px) for a new one. To add/reorder
*backup* targets without replacing the primary, use the endpoint below
instead.

### `POST /config/printers/{logical_name}/targets`

```json
{
  "targets": [
    { "type": "windows", "windows_printer_name": "EPSON TM-T20III Receipt" },
    { "type": "network", "host": "192.168.1.51", "port": 9100 }
  ]
}
```

Replaces an existing mapping's ordered **failover** target list wholesale -
index 0 is the primary, tried first; each one after it is only tried once
everything above it has exhausted its own retries (see
[Architecture notes](#architecture-notes)). Always resubmit the complete
list (add/remove/reorder client-side, then send it all) rather than a
single incremental change - this is what the Printers page's "Manage
targets" dialog does. `404` if `logical_name` isn't an existing mapping
(unlike `POST /config/printers`, this never creates one); `400` if
`targets` is empty or any entry is invalid (e.g. an unrecognized Windows
printer name).

### `GET /config/printers/{logical_name}/status`

```json
{ "logical_name": "receipt_printer_1", "status": "ready" }
```

Live status for one mapping's **primary target only** (`targets[0]`) - a
backup target being down doesn't by itself make the mapping unable to
print (that's the point of failover), so this deliberately doesn't reflect
backup health. What the status means depends on that target's type:

| Type | Possible values | What it means |
|---|---|---|
| `windows` | `ready`, `offline`, `paper_out`, `paper_jam`, `door_open`, `paused`, `busy`, `error` | the real Windows spooler-reported printer status |
| `network` | `reachable`, `unreachable` | a short TCP connect probe only - there's no universal cross-brand ESC/POS status query over a raw socket, so this can't tell you *why* it's unreachable, just that it is |

`GET /config` includes this same status inline for every mapping (computed
once, in parallel, so the Printers page's first paint doesn't need one
request per row) - this endpoint is for the page's periodic per-row refresh
after that.

### `DELETE /config/printers/{logical_name}`

Removes a mapping. Idempotent - deleting a name that isn't mapped still
returns `200 {"ok": true, ...}`, it just has nothing to do.

### `POST /config/startup`

```json
{ "enabled": true }
```

Creates/removes the same Startup-folder shortcut as the tray menu's "Start
with Windows" checkbox (see app/startup.py) - takes effect immediately, no
restart needed. `GET /config`'s `startup_enabled` field reflects the current
state.

### Auth

If `auth_token` is set in `config.json` (or via the Settings page), every
`/print/*`, every `/config/*`, and `GET /logs` request must include header
`X-Print-Bridge-Token: <token>` or it gets `401`. Leave it unset for local
dev/testing - anyone who can reach `127.0.0.1` on this machine can already run
arbitrary code here, so the token mainly protects against a browser tab
silently POSTing to the bridge; see the CORS section below for the other
half of that protection.

### Rate limiting

Every `/print/*` endpoint is capped at `rate_limit_per_minute` requests per
minute (default `60`, set on the Settings page or via `POST
/config/settings`) - a simple in-memory sliding window, no external
dependency, that resets on restart. Exceeding it returns `429` with a
`Retry-After` header (seconds until the next request would be allowed) and
a JSON `detail` explaining the limit - the printer/spooler never even sees
the request.

The bucket is keyed by the configured auth token if one is set (so it's
one shared allowance across every caller that knows the token), otherwise
by the request's `Origin` header (so distinct browser callers get separate
allowances when there's no token to key on instead). This only guards
against a flood of requests reaching the printer - it's not a general API
gateway, and the limit is intentionally generous by default for normal
receipt-printing traffic.

## Which format should I use?

- **`/print/text`** - fast, simple, best for receipts generated dynamically
  server-side with no complex layout (a list of line items, a barcode, a QR
  code). No file to generate or upload, just JSON.
- **`/print/pdf`** - best when the content has logos, tables, mixed
  languages, or complex formatting. Since the PDF's own layout engine
  already handles text shaping/RTL/font embedding correctly at
  PDF-authoring time, this endpoint gets correct multilingual rendering "for
  free," with zero bridge-side language logic - see
  [Architecture notes](#architecture-notes).
- **Not supported, on purpose: a "send raw HTML" endpoint.** Reliably
  rasterizing arbitrary HTML/CSS would mean bundling a full browser engine
  (Chromium or similar) into a tool that's meant to stay a small,
  single-file background service - a poor trade-off for what a receipt
  actually needs. Render your HTML to a PDF (any normal library/print-to-PDF
  flow) and use `/print/pdf` instead.

## CORS and Chrome's Local Network Access prompt

Print Bridge only allows cross-origin requests from origins you explicitly
list in `allowed_origins` (exact match, e.g. `https://ourapp.example.com`) -
there is no wildcard `*` for the print/config endpoints, because anything on
your LAN could otherwise trigger a print. A page opened via `file://` is
always allowed too (browsers send `Origin: null` for these; only someone who
already has such a page on their own disk could trigger this, so it's not a
real widening of the attack surface) - handy for a quick local HTML test
file with no server or build step.

Starting with Chrome 142, a **public** HTTPS page calling into a server on
`localhost` also has to pass a **Private Network Access (PNA)** preflight,
separate from ordinary CORS. Print Bridge handles this: whenever a request
(especially the `OPTIONS` preflight) carries
`Access-Control-Request-Private-Network: true`, the response includes
`Access-Control-Allow-Private-Network: true` alongside the normal
`Access-Control-Allow-Origin`/`-Methods`/`-Headers` headers.

What this means in practice: the **first** time your `https://` site calls
the bridge in a given Chrome profile, the user sees a one-time permission
prompt - *"ourapp.example.com wants to access devices on your local
network"* (or similar wording). This is expected and is Chrome's doing, not
a bug in Print Bridge; there is nothing the server can do to skip it for an
individual, unmanaged browser. If the request still silently fails with no
prompt and no printed output, check:

- `allowed_origins` in config.json actually contains your site's exact
  origin (scheme + host + port), and you restarted Print Bridge after
  editing it.
- `chrome://settings/content/localNetworkAccess` - the user hasn't
  previously denied the prompt for your site.

On **managed/enterprise** machines, IT can pre-approve this so end users
never see the prompt, via the Chrome policy `LocalNetworkAccessAllowedForUrls`
(list your site's origin(s) there). See Chrome's enterprise policy docs for
how to deploy it (registry/GPO on Windows, or your MDM of choice).

## "Start with Windows"

The tray menu's "Start with Windows" toggle (and the Settings page's
equivalent switch, via `POST /config/startup`) creates/removes a `.lnk`
shortcut in the current user's Startup folder (`shell:startup`, i.e.
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) pointing at the
built `.exe` (or, when running from source, at `pythonw.exe run.py`, so no
console window flashes at login). See app/startup.py.

## Architecture notes

A few decisions here aren't obvious from the code alone:

**Why Arabic/RTL is rendered as an image, not printer-native text
(`/print/test`, `/print/text`, and indirectly `/print/pdf`):** ESC/POS
printers don't reliably shape Arabic (joining letters into their contextual
initial/medial/final forms) or reorder it right-to-left in text mode - even
printers whose codepage table includes Arabic glyphs typically just print
isolated letterforms in physical left-to-right order, which reads as broken
Arabic. The reliable, printer-model-agnostic fix (what real-world
Arabic-market POS systems actually do) is to render the whole thing as a
bitmap: shape + bidi-reorder the text in software (`arabic_reshaper` +
`python-bidi`), draw it with a Unicode TrueType font (PIL/Pillow), and send
the result as one ESC/POS raster image (`GS v 0` - see
`app/escpos_jobs.py`'s `_send_image()`) - that bypasses printer codepage/
firmware support entirely, since it's just dots by the time it reaches the
printer. These primitives (font loading, Arabic detection/shaping,
word-wrap, single-line rasterization) live in `app/text_render.py`, shared
by three callers: the test print's fixed EN/AR content, `/print/text`'s
per-line routing (each line is checked for Arabic independently - a mixed
receipt keeps the fast native path for its English lines and only pays for
image rendering on the ones that need it), and `/print/text`'s dry-run
preview, which lays out every line the same way whether or not it's
Arabic. A line's width/height scale and bold are approximated as an image
transform (stroke width, horizontal/vertical stretch, shrink-to-fit if a
heavily-scaled line would otherwise run off the edge) rather than
pixel-matching the printer's own built-in font - good enough to check a
receipt's layout, not a full ESC/POS emulator.

**Why fonts are loaded with Pillow's `BASIC` layout engine explicitly
(`app/text_render.py`'s `load_font()`):** left unset, `ImageFont.truetype()`
silently switches to Raqm (HarfBuzz-based complex text shaping) instead
whenever the current Pillow build happens to have it available - and that
availability is not consistent across environments for the exact same
Pillow package: observed `False` running this app directly from a plain
venv, but `True` for the identical wheel once bundled into a
PyInstaller-frozen build (whether Raqm's DLL gets discovered depends on
the build environment, not anything this app controls). That matters
because this pipeline already shapes and bidi-reorders Arabic by hand
before any text reaches a font (see the note above) and hands PIL text
that's already in final, visually-ordered presentation-form glyphs. Raqm,
given that already-shaped text, has no way to know it isn't plain
logical-order input and tries to shape/reorder it a *second* time -
corrupting it into disconnected, garbled letterforms. This was reproduced
directly: a minimal script frozen with PyInstaller rendered Arabic
correctly with `layout_engine=ImageFont.Layout.BASIC` forced, and
incorrectly with the engine left to Pillow's default, on the same machine,
with the same font files - explaining why the exact same source could
render correctly on one machine/build and break on another with no code
change. Forcing `BASIC` makes rendering deterministic regardless of
whether Raqm happens to be present.

**Why PDF printing rasterizes the page rather than interpreting PDF
content directly (`/print/pdf`, `app/pdf_jobs.py`):** a PDF's own layout
engine has already solved text shaping, RTL, font embedding/substitution,
tables, and mixed scripts correctly at authoring time. Reimplementing any of
that server-side (translating PDF content streams into ESC/POS text
commands) would mean re-solving problems PDF already solved, and would
still fail for anything ESC/POS text mode can't represent (logos, complex
layout). Treating each page as a fixed image sidesteps all of that - and
reuses the exact same raster-image ESC/POS code path as the combined test
print (`app.escpos_jobs.run_pdf_job` shares its `_send_image()` primitive
with `run_test_print_job`), so there's one implementation of "send a bitmap
to the printer" to get right, not two.

**Why printer mappings go through one `open_connector()` (`app/printers.py`)
regardless of Windows vs. network:** `Win32Raw` (an installed Windows
printer) and `escpos.printer.Network` (a raw TCP ESC/POS printer, no driver
needed) are both python-escpos connectors and share the same write API
(`.text()`, `.image()`, `.cut()`, ...) since both subclass
`escpos.printer.Escpos`. Resolving a mapping to a connector in exactly one
place means every job type (text/raw/test-print/PDF, `app/escpos_jobs.py`)
is written once and works identically against either backend, rather than
branching on printer type throughout the job code.

**Why a failed job is retried by resending the whole thing, not just the
failing part (`app/escpos_jobs.py`'s `_run_job_with_retry`):** ESC/POS has
no concept of resuming a partially-sent job - if a 5-page PDF fails after 3
pages went through, the printer has no "continue from page 4" - so a retry
opens a fresh connector and resends everything from the top. Only
transport-level failures that plausibly clear up on their own (spooler
busy, a network printer refusing/timing out while mid-reboot) are retried,
up to 3 times with backoff (0.5s, 1.5s, 4s); a configuration problem (an
unknown printer type, a malformed mapping) fails immediately instead, since
retrying it three times would just waste time re-failing the same way.
Either outcome - eventual success or exhausting all retries - records how
many attempts it took in the job log (`GET /logs`), so a printer that's
flaky but self-healing doesn't look identical to one that never had a
problem.

**Why failover only kicks in after a target's retries are exhausted, not
on the first failure (`app/escpos_jobs.py`'s `_run_job_with_failover`):** a
mapping's `targets` list is ordered (primary first, then backups), and each
target already gets its own retry-with-backoff (above) before failover
ever considers moving on. A momentary spooler hiccup on the primary should
recover on the primary, not immediately hand the job to a backup that then
"wins" every time the primary blinks - failover is for when a target is
actually down, not merely slow. Every target attempted contributes to the
job's total `attempts`, and whichever target actually completes the job is
recorded as `served_by` in the job log - so "the primary was unplugged and
the backup handled it" is visible after the fact, not just inferred from a
temporary warning in the log file. Copies (`/print/text`'s `copies` field)
attempt the target list independently per copy, so a primary that comes
back between copies is preferred again rather than the whole job "sticking"
to whichever target served the first one.

**Why rate limiting is in-memory and process-local, not backed by a
database or shared store (`app/rate_limit.py`):** Print Bridge only ever
runs as a single local process on one machine (see `BridgeServer` in
`app/main.py`) - there's no second instance or worker process for a shared
store to coordinate with, so a plain per-process sliding-window counter is
exactly as durable as anything else here (all in-memory, gone on restart,
same as everything else that isn't `config.json` or the job-history
SQLite file). Keyed by auth token if one's configured, otherwise by the
request's `Origin` header (see `app/main.py`'s `enforce_rate_limit`) -
enough to stop one runaway caller from flooding the spooler without
needing per-IP tracking or anything heavier.

**Why the CORS middleware explicitly handles the Private Network Access
preflight header:** Chrome 142+ requires it for any public HTTPS origin
calling into `127.0.0.1`, on top of ordinary CORS (see the CORS section
above) - without echoing `Access-Control-Allow-Private-Network: true` back,
every request from a real deployed website would be silently blocked by the
browser before it ever reaches Print Bridge, regardless of `allowed_origins`
being configured correctly. See `create_app()`'s `cors_and_private_network`
middleware in `app/main.py` for the implementation and inline commentary.

## Building

```
.venv\Scripts\pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
.venv\Scripts\python build.py
```

Building is two steps because the frontend and the Python app are built by
different toolchains: `npm run build` compiles `frontend/` into static
`frontend/dist/` (plain HTML/JS/CSS, no runtime Node dependency after this
point), then `python build.py` generates `icon.ico` (if it doesn't already
exist) and runs PyInstaller against `build.spec`, which bundles
`frontend/dist/` into the .exe as app data (`build.spec` refuses to run if
`frontend/dist/` is missing, with a message pointing back at the `npm run
build` step, rather than silently shipping a broken UI).

The result is a single standalone `dist\PrintBridge.exe` that runs on a
machine with no Python or Node installed. It's a windowed (no console) app -
if it fails to start (e.g. the configured port is already in use), that's
reported via a message box and always logged to
`%APPDATA%\PrintBridge\logs\print_bridge.log`.

## Testing it end-to-end

1. `GET http://localhost:9187/printers` should list your real Windows
   printers.
2. Map one to a logical name on the **Printers** page; restart Print Bridge;
   confirm the mapping is still there (it's in
   `%APPDATA%\PrintBridge\config.json`, independent of the process). Remove
   it via the row's actions menu -> "Remove" (confirms via a dialog) and
   confirm it disappears - both from the table and from `GET /config`.
3. Open the row's actions menu (the "..." button, top right of the row) and
   use "Test print" - on a real ESC/POS printer you should get a
   connectivity header, a horizontal rule, the bilingual EN/AR test quotes,
   a QR code, and a paper cut, all as one job. It's the best single check
   that a mapping works at all *and* that non-Latin text (and image
   printing in general) renders correctly, since that's the part most
   likely to silently break on a given printer. Without hardware, Windows'
   "Microsoft Print to PDF" will still accept the raw job (so you can
   confirm the plumbing works end to end) but obviously won't render it as
   a receipt.
4. Try "Print PDF" with a real multi-page PDF, ideally including a page
   with Arabic (or any non-Latin) text - it should print page by page,
   cutting between pages, proving the raster pipeline is shared correctly
   between the test-print path and PDF printing.
5. Click "Preview" (a dry-run sample English/Arabic receipt) and "Preview
   PDF" (pick the same PDF from step 4) - both should show the rendered
   image in a dialog immediately, and neither should print anything or add
   a row to the Logs page.
6. Check the **Logs** page - every real attempt from steps 3-4 (success and
   any failures, with their error messages and attempt count) should show
   up there, newest first - the dry-run previews from step 5 should not.
7. If you have a network-capable printer, add it as a **Network printer**
   mapping (its IP + ESC/POS port) instead of picking one from the Windows
   dropdown, and repeat step 3 - it should print identically. Then power it
   off and watch its status badge flip to "Unreachable" within ~15s; power
   it back on and confirm the badge recovers. A job sent while it's briefly
   unreachable should show up in the Logs page with more than one attempt.
8. Open a mapping's actions menu (the "..." button) -> **Manage targets**
   and add a second target as a backup (e.g. a network printer that's
   currently powered off, or any address nothing is listening on). Send a
   test print: it should still succeed, the Logs page's "Served by" column
   showing the backup rather than the primary, with a higher attempt count
   (the primary's retries had to exhaust first). Reorder the targets
   (backup to primary) and print again - it should now succeed immediately
   on what's now the primary, attempts back down to 1.
9. On the **Settings** page, lower "Rate limit (req/min)" to something
   small (e.g. `3`) and save - it takes effect immediately, no restart.
   Fire more than that many `/print/text` requests within a minute (e.g.
   the Preview button a few times fast) and confirm you get a `429` once
   you're over the limit, instead of a pile of print jobs. Set it back to
   a normal value (e.g. `60`) afterward.
10. Host your own site over `https://` and call `/print/text` or
    `/print/pdf` from it - watch for the one-time Chrome permission prompt
    described above.

## Project layout

```
app/
  main.py            FastAPI app, CORS/PNA middleware, rate limiting, routes, threaded uvicorn server
  printers.py         win32print enumeration, Windows/network connector resolution, status
  escpos_jobs.py       builds/sends ESC/POS jobs via python-escpos, with retry + failover
  rate_limit.py         in-memory sliding-window rate limiter (no external dependency)
  pdf_jobs.py            rasterizes an uploaded PDF (PyMuPDF + Pillow dithering)
  test_print.py            combined connectivity + EN/AR test print, rendered as a bitmap
  text_render.py             shared font/Arabic-shaping/line-rasterization primitives
  job_log.py                   SQLite-backed print-job history (GET /logs)
  config.py                      load/save %APPDATA%\PrintBridge\config.json
  startup.py                       "Start with Windows" shortcut management
  tray.py                            pystray icon, menu, Startup-folder toggle
frontend/
  src/
    App.tsx              layout: sidebar nav + view switch (no router)
    pages/                  PrintersPage.tsx / LogsPage.tsx / SettingsPage.tsx
    components/               shadcn/ui primitives (components/ui) + ConfirmDialog/PreviewDialog/TargetsDialog
    hooks/usePrintBridge.ts     data-fetching hooks (health/printers/config/logs/status)
    lib/api.ts                   typed fetch wrapper for the HTTP API
  dist/                           npm run build output - bundled into the .exe
run.py                  entry point: starts the server thread, then the tray icon
build.spec / build.py     PyInstaller packaging (bundles frontend/dist/)
```

Every module above has a top-of-file docstring explaining its
responsibility, and every public function/class has one describing its
purpose, parameters, and return value - see the module itself for details
beyond what's summarized here.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
