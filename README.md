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
- **Correct Arabic/RTL rendering** - a combined connectivity + bilingual
  English/Arabic test print proves shaping and right-to-left order come out
  right, not just isolated letterforms.
- **A real config UI** (dark-mode React app) for mapping logical printer
  names to real Windows printers, watching print-job history, and managing
  server settings - no hand-editing JSON required.
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
2. Pick a Windows printer from the dropdown, give it a logical name (e.g.
   `receipt_printer_1`), click "Save mapping". Your webpage will refer to
   printers by this logical name, never the real Windows printer name - that
   indirection is what lets you repoint `receipt_printer_1` at a different
   physical printer on a different till without changing any app code.
   DPI/width are optional and only matter for PDF printing - see
   [`POST /print/pdf`](#post-printpdf).
3. Use the row's "Test print" / "Print PDF" buttons to confirm it actually
   prints (see [Testing it end-to-end](#testing-it-end-to-end)).
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
| GET | `/config` | if token set | current port/origins/printer mappings/startup state |
| POST | `/config/printers` | if token set | add/update a logical->Windows printer mapping |
| DELETE | `/config/printers/{logical_name}` | if token set | remove a mapping (idempotent) |
| POST | `/config/settings` | if token set | update port/allowed_origins/auth_token (restart required) |
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
    { "text": "1x Croissant        $4.00", "align": "left" }
  ],
  "barcode": { "type": "code128", "data": "0123456789" },
  "qr": { "data": "https://example.com/order/123" },
  "cut": true,
  "open_drawer": false,
  "copies": 1
}
```

`printer` is the **logical name** from step 2 above, resolved server-side to
the real Windows printer. `align` is `left`/`center`/`right`; `width`/`height`
are text scale 1-8; `barcode`, `qr`, `open_drawer` are all optional.
`barcode.type` accepts the usual ESC/POS symbologies (`CODE128`, `CODE39`,
`EAN13`, `EAN8`, `UPC-A`, `ITF`, ... - case-insensitive).

Responses: `{"ok": true, "job_id": "..."}` on success; `404` if the logical
printer name isn't configured; `502` (with the underlying Windows/spooler
error as the detail) if the printer is offline, paused, or otherwise
rejected the job. Every attempt (success or failure) is written to both the
log file and `GET /logs`.

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
button in the config UI and in `test_page.html`; besides confirming a
mapping prints at all, it's also the best single check that non-Latin text
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

### `GET /logs`

```json
{ "jobs": [
  { "timestamp": 1712345678.9, "endpoint": "print/pdf", "printer": "receipt_printer_1",
    "ok": true, "error": null, "job_id": "..." },
  { "timestamp": 1712345670.1, "endpoint": "print/text", "printer": "till_2",
    "ok": false, "error": "Windows printer 'till_2 (offline)' is not available: ...", "job_id": "..." }
]}
```

The last ~200 attempts across every `/print/*` endpoint, newest first,
backed by a small SQLite file (`%APPDATA%\PrintBridge\logs\jobs.db`) so it
survives a restart. Optional `?limit=N` query param. Powers the Logs page.

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
already has the test page on their own disk could trigger this, so it's not
a real widening of the attack surface) - this is what makes `test_page.html`
work unmodified straight from disk.

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
(`/print/test`, and indirectly `/print/pdf`):** ESC/POS printers
don't reliably shape Arabic (joining letters into their contextual
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
printer.

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
   it via the row's trash-can button (confirms via a dialog) and confirm it
   disappears - both from the table and from `GET /config`.
3. Use the row's "Test print" button, or open `test_page.html` directly
   from disk (double-click it, or drag it into Chrome) and click "Send test
   print" - on a real ESC/POS printer you should get a connectivity header,
   a horizontal rule, the bilingual EN/AR test quotes, a QR code, and a
   paper cut, all as one job. It's the best single check that a mapping
   works at all *and* that non-Latin text (and image printing in general)
   renders correctly, since that's the part most likely to silently break
   on a given printer. Without hardware, Windows' "Microsoft Print to PDF"
   will still accept the raw job (so you can confirm the plumbing works end
   to end) but obviously won't render it as a receipt. Also try "Send
   example receipt" (`test_page.html` only) for a look at the plain
   `/print/text` JSON API (line items, barcode, QR).
4. Try "Print PDF" with a real multi-page PDF, ideally including a page
   with Arabic (or any non-Latin) text - it should print page by page,
   cutting between pages, proving the raster pipeline is shared correctly
   between the test-print path and PDF printing.
5. Check the **Logs** page - every attempt from steps 3-4 (success and any
   failures, with their error messages) should show up there, newest first.
6. Host `test_page.html` (or your real site) over `https://` somewhere and
   repeat - watch for the one-time Chrome permission prompt described above.

## Project layout

```
app/
  main.py            FastAPI app, CORS/PNA middleware, routes, threaded uvicorn server
  printers.py         win32print enumeration + logical->real name resolution
  escpos_jobs.py       builds/sends ESC/POS jobs via python-escpos's Win32Raw
  pdf_jobs.py            rasterizes an uploaded PDF (PyMuPDF + Pillow dithering)
  test_print.py            combined connectivity + EN/AR test print, rendered as a bitmap
  job_log.py                SQLite-backed print-job history (GET /logs)
  config.py                  load/save %APPDATA%\PrintBridge\config.json
  startup.py                   "Start with Windows" shortcut management
  tray.py                       pystray icon, menu, Startup-folder toggle
frontend/
  src/
    App.tsx              layout: sidebar nav + view switch (no router)
    pages/                  PrintersPage.tsx / LogsPage.tsx / SettingsPage.tsx
    components/               shadcn/ui primitives (components/ui) + ConfirmDialog
    hooks/usePrintBridge.ts     data-fetching hooks (health/printers/config/logs)
    lib/api.ts                   typed fetch wrapper for the HTTP API
  dist/                           npm run build output - bundled into the .exe
run.py                  entry point: starts the server thread, then the tray icon
build.spec / build.py     PyInstaller packaging (bundles frontend/dist/)
test_page.html               standalone fetch() example (file:// and https://)
```

Every module above has a top-of-file docstring explaining its
responsibility, and every public function/class has one describing its
purpose, parameters, and return value - see the module itself for details
beyond what's summarized here.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
