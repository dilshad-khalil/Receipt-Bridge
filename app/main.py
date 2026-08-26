"""FastAPI application for Print Bridge.

`create_app()` builds a fresh app instance from whatever is currently in
config.json - it's called once at import time (module-level `app`, used by
`uvicorn app.main:app` during development) and again by `BridgeServer` every
time the tray's "Restart server" is used, so port/allowed_origins changes
made through the config UI take effect on restart without a full process
relaunch.
"""
from __future__ import annotations

import base64
import io
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, Optional

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from app import __version__
from app import config as config_mod
from app import escpos_jobs, job_log, pdf_jobs, printers, rate_limit, startup, test_print, text_render

# ---------------------------------------------------------------------------
# Paths (dev checkout vs. PyInstaller onefile bundle)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Built by `npm run build` inside frontend/ (see README "Building") - a Vite/
# React/shadcn single-page app. Not part of source control as build output;
# create_app() falls back to a plain "not built yet" response if it's missing
# so `python run.py` from a fresh checkout fails obviously rather than 404ing.
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

JOB_NAME_PREFIX = "PrintBridge"
# Printed as a QR code at the end of the combined "Test print" job - just a
# recognizable, harmless payload to confirm QR rendering works, not a real
# endpoint the printer or anything else needs to reach.
TEST_PRINT_QR_DATA = "https://example.com/print-bridge-test"

logger = logging.getLogger("print_bridge")
_logging_configured = False


def setup_logging() -> None:
    """Configure the rotating file logger. Safe to call more than once."""
    global _logging_configured
    if _logging_configured:
        return
    log_path = config_mod.get_logs_dir() / "print_bridge.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _logging_configured = True


setup_logging()

# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

Align = str  # "left" | "center" | "right", validated in escpos_jobs


class LineItem(BaseModel):
    """One line of text within a `POST /print/text` request body."""

    text: str = ""
    align: str = "left"
    bold: bool = False
    width: int = Field(default=1, ge=1, le=8)
    height: int = Field(default=1, ge=1, le=8)


class BarcodeSpec(BaseModel):
    """Optional barcode block within `POST /print/text`."""

    type: str
    data: str


class QRSpec(BaseModel):
    """Optional QR-code block within `POST /print/text`."""

    data: str


CutMode = Literal["none", "partial", "full"]


class PrintTextRequest(BaseModel):
    """Body for `POST /print/text` - structured receipt content: lines of
    text, an optional barcode/QR code, and cut/cash-drawer/copies options.

    `dry_run: true` skips the printer entirely and returns a rendered
    preview image instead (see the /print/text handler) - useful for
    checking a receipt's layout (and Arabic shaping) before committing
    paper to it.
    """

    printer: str
    lines: list[LineItem] = []
    barcode: Optional[BarcodeSpec] = None
    qr: Optional[QRSpec] = None
    cut: CutMode = "none"
    open_drawer: bool = False
    copies: int = Field(default=1, ge=1, le=20)
    dry_run: bool = False

    @field_validator("cut", mode="before")
    @classmethod
    def _normalize_cut(cls, value: Any) -> Any:
        """Accept the pre-v4b boolean `cut` field transparently, so
        existing integrations sending `true`/`false` keep working
        unchanged: `true` -> `"full"`, `false` -> `"none"`. Runs before
        the `CutMode` Literal validation, so a string value just passes
        through untouched."""
        if isinstance(value, bool):
            return "full" if value else "none"
        return value


class PrintRawRequest(BaseModel):
    """Body for `POST /print/raw` - caller-built raw ESC/POS bytes, base64-
    encoded, written straight to the printer with no interpretation."""

    printer: str
    data_base64: str


class PrintTestRequest(BaseModel):
    """Body for `POST /print/test` - the combined connectivity + bilingual
    EN/AR test print (see app/test_print.py)."""

    printer: str
    width_px: int = Field(default=384, ge=128, le=1024)  # 384=58mm, 576=80mm printers
    cut: bool = True


class PrinterMappingRequest(BaseModel):
    """Body for `POST /config/printers` - add or update a logical printer
    mapping. `type` selects which of the two target shapes the rest of the
    fields use: `"windows"` needs `windows_printer_name` (an installed,
    driver-backed printer); `"network"` needs `host` (and optionally
    `port`, default 9100) - a raw ESC/POS printer reachable over TCP,
    needing no Windows driver/installation at all."""

    logical_name: str
    type: Literal["windows", "network"] = "windows"
    windows_printer_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    # None = "leave unchanged if this mapping already exists, else use the
    # module default" (see config.upsert_printer) - lets the UI resubmit
    # this form to repoint a mapping at a different target without having
    # to know/resend its current DPI/width_px.
    dpi: Optional[int] = Field(default=None, ge=72, le=1200)
    width_px: Optional[int] = Field(default=None, ge=128, le=1024)


class TargetSpec(BaseModel):
    """One entry in a `POST /config/printers/{logical_name}/targets`
    request body - the same per-target shape as `PrinterMappingRequest`,
    minus `dpi`/`width_px` (those are mapping-level, not per-target - see
    app/config.py's `_normalize_mapping`)."""

    type: Literal["windows", "network"] = "windows"
    windows_printer_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)


class TargetsUpdateRequest(BaseModel):
    """Body for `POST /config/printers/{logical_name}/targets` - the full,
    ordered list of targets to fail over across for an *existing* mapping,
    index 0 first (primary). Used by the Printers page's target-editor
    dialog whenever a target is added, removed, or reordered - always
    resubmits the complete list rather than a single incremental change."""

    targets: list[TargetSpec] = Field(min_length=1)


class SettingsUpdateRequest(BaseModel):
    """Body for `POST /config/settings`. All fields optional - only the ones
    actually present in the request are changed (see `model_fields_set`
    usage in update_settings() below), so a partial update can't
    accidentally clear an untouched field."""

    port: Optional[int] = Field(default=None, ge=1, le=65535)
    allowed_origins: Optional[list[str]] = None
    auth_token: Optional[str] = None
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1, le=6000)


class StartupUpdateRequest(BaseModel):
    """Body for `POST /config/startup` - the Settings page's "Start with
    Windows" toggle."""

    enabled: bool


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def require_token(request: Request) -> None:
    """Gate /print/* and /config/* behind X-Print-Bridge-Token, if configured.

    Config is re-read from disk on every call (cheap - it's a small local
    JSON file) so that changing/clearing the token via the config UI takes
    effect immediately, without requiring a server restart.
    """
    cfg = config_mod.load_config()
    token = cfg.get("auth_token")
    if not token:
        return
    supplied = request.headers.get("x-print-bridge-token")
    if supplied != token:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid X-Print-Bridge-Token header",
        )


def enforce_rate_limit(request: Request) -> None:
    """Abuse guard for /print/* - default 60 requests/minute, configurable
    via config.json's `rate_limit_per_minute` (see the Settings page),
    sliding-window, in-memory (see app/rate_limit.py - no external
    dependency; this only ever runs as one local process).

    Keyed by the configured auth token if one is set - by the time this
    runs, `require_token` (see its `Depends()` ordering on each route
    below) has already confirmed the caller supplied a matching one, so
    this just buckets by that shared value - otherwise by the request's
    `Origin` header, so distinct browser callers don't share one bucket
    when there's no token to key on instead. A request with neither (no
    auth configured, no Origin header - a bare server-to-server call)
    falls back to one shared bucket for "no origin" callers.

    Config is re-read from disk on every call (same reasoning as
    `require_token`) so a `rate_limit_per_minute` change from the Settings
    page takes effect immediately, no restart needed.
    """
    cfg = config_mod.load_config()
    limit = cfg.get("rate_limit_per_minute") or config_mod.DEFAULT_RATE_LIMIT_PER_MINUTE
    token = cfg.get("auth_token")
    key = f"token:{token}" if token else f"origin:{request.headers.get('origin') or 'none'}"
    allowed, retry_after = rate_limit.limiter.check(key, limit)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} requests/minute). Try again shortly.",
            headers={"Retry-After": str(int(retry_after))},
        )


def _record_job(
    endpoint: str,
    printer: str,
    ok: bool,
    error: Optional[str] = None,
    job_id: Optional[str] = None,
    attempts: int = 1,
    served_by: Optional[str] = None,
) -> None:
    """Best-effort write to the job history (GET /logs, the Logs page).

    Never raises - a logging failure (e.g. a locked/corrupt jobs.db) must
    never turn a print job that actually succeeded into an error response.
    """
    try:
        job_log.record(
            endpoint, printer, ok, error=error, job_id=job_id, attempts=attempts, served_by=served_by
        )
    except Exception:
        logger.exception("Failed to write job history entry")


def _image_to_base64_png(image: Image.Image) -> str:
    """Encode a rendered page/line image as a base64 PNG string, for the
    `dry_run` preview responses on /print/text and /print/pdf - the
    printer is never touched, so this is the only way the caller gets to
    see what would have been printed."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _printer_statuses(printers_cfg: dict[str, Any]) -> dict[str, str]:
    """Compute printers.get_printer_status() for every mapping concurrently.

    Run in a small thread pool (rather than one after another) so a slow or
    unreachable network printer's ~1.5s probe timeout doesn't add up
    sequentially across N mappings every time GET /config is called (page
    load, and this endpoint's own occasional refresh) - the whole batch
    takes as long as the single slowest probe, not the sum of all of them.
    """
    if not printers_cfg:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(printers_cfg))) as pool:
        futures = {
            name: pool.submit(printers.get_printer_status, mapping)
            for name, mapping in printers_cfg.items()
        }
        return {name: future.result() for name, future in futures.items()}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build a fresh FastAPI app from the current config.json.

    Called once at import time (module-level `app`) and again on every
    `BridgeServer.restart()`, so config changes that need a restart
    (port, allowed_origins) are picked up without a full process relaunch.
    """
    cfg = config_mod.load_config()
    allowed_origins = set(cfg.get("allowed_origins") or [])
    # A file:// page has an opaque origin, sent by browsers as the literal
    # string "null". Always accepted so a plain local HTML file (no server,
    # no build step) can call the bridge unmodified - only someone who
    # already has local file access could load such a page in the first
    # place, so this doesn't widen the real attack
    # surface (unlike allowing "*", which would let any LAN site through).
    allowed_origins.add("null")

    app = FastAPI(title="Print Bridge", version=__version__)

    @app.middleware("http")
    async def cors_and_private_network(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Hand-rolled CORS + Chrome Private Network Access (PNA) handling.

        Not Starlette's built-in CORSMiddleware, because that doesn't know
        about PNA. Two things happen here, layered on top of each other:

        1. **CORS** - only origins in `allowed_origins` (plus the literal
           string "null", which is what browsers send as `Origin` for a
           `file://` page) get `Access-Control-Allow-Origin` echoed back.
           No wildcard `*` is ever used, because this API can trigger a
           physical print - anything on the LAN being able to call it
           would be a real problem, not just a browser-security nicety.
        2. **Private Network Access** - starting with Chrome 142, a page
           served from a *public* origin (e.g. `https://ourapp.example.com`)
           calling into `127.0.0.1` also has to pass this extra preflight
           check, on top of ordinary CORS. Chrome marks such a preflight
           with `Access-Control-Request-Private-Network: true`; the server
           must answer with `Access-Control-Allow-Private-Network: true` or
           the browser blocks the request (the user sees a one-time "wants
           to access devices on your local network" permission prompt the
           first time this succeeds - see the README's CORS section).

        Both checks are evaluated for every request (not just OPTIONS)
        because a real cross-origin request also carries `Origin` and needs
        the same response headers on its actual response, not just on the
        preflight.
        """
        origin = request.headers.get("origin")
        origin_allowed = origin is not None and origin in allowed_origins
        wants_private_network = (
            request.headers.get("access-control-request-private-network", "").lower()
            == "true"
        )
        is_preflight = (
            request.method == "OPTIONS"
            and request.headers.get("access-control-request-method") is not None
        )

        if is_preflight:
            headers: dict[str, str] = {}
            if origin_allowed:
                headers["Access-Control-Allow-Origin"] = origin  # type: ignore[assignment]
                headers["Vary"] = "Origin"
                headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                requested_headers = request.headers.get("access-control-request-headers")
                headers["Access-Control-Allow-Headers"] = (
                    requested_headers or "Content-Type, X-Print-Bridge-Token"
                )
                headers["Access-Control-Max-Age"] = "600"
            if wants_private_network:
                headers["Access-Control-Allow-Private-Network"] = "true"
            return Response(status_code=204, headers=headers)

        response = await call_next(request)
        if origin_allowed:
            response.headers["Access-Control-Allow-Origin"] = origin  # type: ignore[assignment]
            response.headers["Vary"] = "Origin"
        if wants_private_network:
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    # --- Health & discovery --------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness check - no auth required, used by run.py to detect an
        already-running instance and by the frontend's status pill."""
        return {"status": "ok", "version": __version__}

    @app.get("/printers")
    def get_printers() -> dict[str, Any]:
        """Windows printers installed/visible to this user session - the
        source list for mapping a logical name to a real printer."""
        return {"printers": printers.list_printers()}

    # --- Config ----------------------------------------------------------

    @app.get("/config", dependencies=[Depends(require_token)])
    def get_config() -> dict[str, Any]:
        """Current server config: port, allowed origins, printer mappings
        (each with a `status` field baked in), and whether auth/startup are
        enabled - powers the Settings and Printers pages.

        Status is computed here (rather than left for the frontend to poll
        per-printer up front) so the Printers page's initial paint doesn't
        need N+1 requests - one per mapping - just to show a status
        indicator. Ongoing updates after that use the lighter single-printer
        GET /config/printers/{logical_name}/status instead of re-running
        this for the whole list every time.
        """
        current = config_mod.load_config()
        printers_cfg = current.get("printers", {})
        statuses = _printer_statuses(printers_cfg)
        printers_with_status = {
            name: {**mapping, "status": statuses.get(name, "unknown")}
            for name, mapping in printers_cfg.items()
        }
        return {
            "port": current["port"],
            "allowed_origins": current.get("allowed_origins", []),
            "printers": printers_with_status,
            "auth_enabled": bool(current.get("auth_token")),
            "rate_limit_per_minute": current.get(
                "rate_limit_per_minute", config_mod.DEFAULT_RATE_LIMIT_PER_MINUTE
            ),
            # Reflects the actual Startup-folder shortcut state (app/startup.py),
            # not a config.json field - this is what the tray's "Start with
            # Windows" checkbox and the Settings page's toggle both read/write.
            "startup_enabled": startup.is_startup_enabled(),
        }

    @app.get("/config/printers/{logical_name}/status", dependencies=[Depends(require_token)])
    def get_printer_status(logical_name: str) -> dict[str, Any]:
        """Live status for one mapping - see printers.get_printer_status for
        what the returned string means per printer type. Powers the
        Printers page's per-row status indicator, polled periodically -
        deliberately a single-printer lookup (not the whole GET /config
        payload) so polling cost scales with how many rows are actually on
        screen, not with re-deriving the entire config every time."""
        cfg = config_mod.load_config()
        try:
            mapping = printers.resolve_printer_settings(logical_name, cfg)
        except printers.UnknownLogicalPrinterError:
            raise HTTPException(
                status_code=404,
                detail=f"No printer configured with logical name '{logical_name}'.",
            )
        return {"logical_name": logical_name, "status": printers.get_printer_status(mapping)}

    @app.post("/config/startup", dependencies=[Depends(require_token)])
    def set_startup(body: StartupUpdateRequest) -> dict[str, Any]:
        """Create/remove the Startup-folder shortcut - the HTTP equivalent of
        the tray menu's "Start with Windows" checkbox, for the Settings page.
        Takes effect immediately; no restart needed."""
        try:
            enabled = startup.set_startup_enabled(body.enabled)
        except Exception as exc:
            logger.exception("Failed to update 'Start with Windows' shortcut")
            raise HTTPException(status_code=500, detail=str(exc))
        logger.info("config: 'Start with Windows' set to %s", enabled)
        return {"ok": True, "startup_enabled": enabled}

    @app.post("/config/printers", dependencies=[Depends(require_token)])
    def add_or_update_printer(body: PrinterMappingRequest) -> dict[str, Any]:
        """Add a new logical printer mapping, or update an existing one's
        target/DPI/width.

        A `"windows"` mapping is rejected up front if the named Windows
        printer isn't currently installed/visible, so a typo doesn't
        silently create a mapping that will always fail to print. A
        `"network"` mapping can't be validated that way - the printer might
        just be temporarily powered off or not yet on the network - so it's
        accepted as given and only surfaces a problem when actually used
        (see the Printers page's status indicator and /print/* endpoints)."""
        if body.type == "windows":
            if not body.windows_printer_name:
                raise HTTPException(
                    status_code=400, detail="windows_printer_name is required for type=windows"
                )
            if not printers.printer_exists(body.windows_printer_name):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Windows printer '{body.windows_printer_name}' was not found. "
                        "Call GET /printers for the current list of installed printers."
                    ),
                )
            cfg = config_mod.upsert_printer(
                body.logical_name,
                printer_type="windows",
                name=body.windows_printer_name,
                dpi=body.dpi,
                width_px=body.width_px,
            )
        else:
            if not body.host:
                raise HTTPException(status_code=400, detail="host is required for type=network")
            cfg = config_mod.upsert_printer(
                body.logical_name,
                printer_type="network",
                host=body.host,
                port=body.port,
                dpi=body.dpi,
                width_px=body.width_px,
            )
        logger.info(
            "config: mapped logical printer '%s' -> %s",
            body.logical_name,
            printers.describe_mapping(cfg["printers"][body.logical_name]),
        )
        return {"ok": True, "printers": cfg["printers"]}

    @app.post(
        "/config/printers/{logical_name}/targets", dependencies=[Depends(require_token)]
    )
    def set_printer_targets(logical_name: str, body: TargetsUpdateRequest) -> dict[str, Any]:
        """Replace an existing mapping's ordered target list wholesale -
        add/remove/reorder backup targets for failover (see
        app/escpos_jobs.py's failover logic; index 0 is always tried
        first, the rest are backups tried in order after it). Powers the
        Printers page's target-editor dialog: every add/remove/reorder
        there resubmits the complete list rather than a single incremental
        change, so this endpoint only ever has to do one thing - validate
        and replace.

        Unlike `POST /config/printers`, this only edits an *existing*
        mapping - it 404s rather than creating a new one, since the
        target-editor dialog this powers only ever opens for a mapping
        already in the table. Windows targets are validated the same way
        `add_or_update_printer` validates its primary target (the named
        printer must currently be installed/visible); network targets
        can't be validated ahead of time the same way (see that handler's
        docstring) so are accepted as given.
        """
        built: list[dict[str, Any]] = []
        for i, t in enumerate(body.targets):
            if t.type == "windows":
                if not t.windows_printer_name:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Target {i + 1}: windows_printer_name is required for type=windows",
                    )
                if not printers.printer_exists(t.windows_printer_name):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Target {i + 1}: Windows printer '{t.windows_printer_name}' was not "
                            "found. Call GET /printers for the current list of installed printers."
                        ),
                    )
                built.append({"type": "windows", "name": t.windows_printer_name})
            else:
                if not t.host:
                    raise HTTPException(
                        status_code=400, detail=f"Target {i + 1}: host is required for type=network"
                    )
                built.append(
                    {"type": "network", "host": t.host, "port": t.port or config_mod.DEFAULT_NETWORK_PORT}
                )

        try:
            cfg = config_mod.set_printer_targets(logical_name, built)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"No printer configured with logical name '{logical_name}'.",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        logger.info(
            "config: updated targets for logical printer '%s' -> %s",
            logical_name,
            printers.describe_mapping(cfg["printers"][logical_name]),
        )
        return {"ok": True, "printers": cfg["printers"]}

    @app.delete("/config/printers/{logical_name}", dependencies=[Depends(require_token)])
    def delete_printer(logical_name: str) -> dict[str, Any]:
        """Remove a logical->Windows printer mapping. Idempotent: removing a
        name that isn't mapped is not an error, it just leaves nothing to do
        - this keeps the frontend's remove action simple (no need to check
        existence first, and a double-click/retry can't fail)."""
        cfg = config_mod.remove_printer(logical_name)
        logger.info("config: removed logical printer mapping '%s'", logical_name)
        return {"ok": True, "printers": cfg["printers"]}

    @app.post("/config/settings", dependencies=[Depends(require_token)])
    def update_settings(body: SettingsUpdateRequest) -> dict[str, Any]:
        """Update port/allowed_origins/auth_token/rate_limit_per_minute.
        Port and allowed_origins need a restart (tray -> "Restart server")
        to take effect, since the HTTP server and CORS middleware are only
        built once, in create_app()/BridgeServer.start() - the auth token
        and rate limit both take effect immediately instead, since
        require_token()/enforce_rate_limit() re-read config on every
        request."""
        fields_set = body.model_fields_set
        cfg = config_mod.update_settings(
            port=body.port if "port" in fields_set else None,
            allowed_origins=body.allowed_origins if "allowed_origins" in fields_set else None,
            auth_token=body.auth_token if "auth_token" in fields_set else config_mod.UNSET,
            rate_limit_per_minute=body.rate_limit_per_minute
            if "rate_limit_per_minute" in fields_set
            else None,
        )
        logger.info("config: settings updated (restart required to take effect)")
        return {
            "ok": True,
            "port": cfg["port"],
            "allowed_origins": cfg.get("allowed_origins", []),
            "auth_enabled": bool(cfg.get("auth_token")),
            "rate_limit_per_minute": cfg.get(
                "rate_limit_per_minute", config_mod.DEFAULT_RATE_LIMIT_PER_MINUTE
            ),
            "restart_required": True,
        }

    # --- Printing ----------------------------------------------------------

    @app.post("/print/text", dependencies=[Depends(require_token), Depends(enforce_rate_limit)])
    def print_text(body: PrintTextRequest) -> dict[str, Any]:
        """Render structured content (lines/barcode/QR/cut/drawer) and print
        it - the main endpoint for simple, dynamically-generated receipts.
        See the README's "Which format should I use?" section for when to
        prefer this over /print/pdf.

        `dry_run: true` renders the same content as a preview PNG (see
        text_render.render_lines_preview) and returns it without ever
        opening a printer connector - not logged as a job, since no print
        was actually attempted."""
        cfg = config_mod.load_config()
        try:
            mapping = printers.resolve_printer_settings(body.printer, cfg)
        except printers.UnknownLogicalPrinterError:
            logger.warning("print/text: unknown logical printer '%s'", body.printer)
            _record_job("print/text", body.printer, ok=False, error="unknown logical printer")
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No printer configured with logical name '{body.printer}'. "
                    "Add one via POST /config/printers."
                ),
            )
        target = printers.describe_mapping(mapping)

        if body.dry_run:
            width_px = mapping.get("width_px", config_mod.DEFAULT_PRINTER_WIDTH_PX)
            try:
                preview = text_render.render_lines_preview(
                    body.lines, width_px, body.barcode, body.qr
                )
            except text_render.FontNotFoundError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
            logger.info("print/text DRY-RUN logical=%s target=%s", body.printer, target)
            return {
                "ok": True,
                "dry_run": True,
                "preview_images_base64": [_image_to_base64_png(preview)],
            }

        job_id = uuid.uuid4().hex
        try:
            attempts, served_by = escpos_jobs.run_text_job(
                mapping, body, job_name=f"{JOB_NAME_PREFIX} - {body.printer}"
            )
        except escpos_jobs.PrintJobError as exc:
            logger.error(
                "print/text FAILED job=%s logical=%s target=%s attempts=%d error=%s",
                job_id, body.printer, target, exc.attempts, exc,
            )
            _record_job(
                "print/text", body.printer, ok=False, error=str(exc), job_id=job_id,
                attempts=exc.attempts,
            )
            raise HTTPException(status_code=502, detail=str(exc))

        logger.info(
            "print/text OK job=%s logical=%s target=%s served_by=%s lines=%d copies=%d attempts=%d",
            job_id, body.printer, target, served_by, len(body.lines), body.copies, attempts,
        )
        _record_job(
            "print/text", body.printer, ok=True, job_id=job_id, attempts=attempts, served_by=served_by
        )
        return {"ok": True, "job_id": job_id}

    @app.post("/print/raw", dependencies=[Depends(require_token), Depends(enforce_rate_limit)])
    def print_raw(body: PrintRawRequest) -> dict[str, Any]:
        """Escape hatch for callers who build their own ESC/POS bytes -
        decoded from base64 and written straight to the printer, no
        interpretation."""
        cfg = config_mod.load_config()
        try:
            mapping = printers.resolve_printer_settings(body.printer, cfg)
        except printers.UnknownLogicalPrinterError:
            logger.warning("print/raw: unknown logical printer '%s'", body.printer)
            _record_job("print/raw", body.printer, ok=False, error="unknown logical printer")
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No printer configured with logical name '{body.printer}'. "
                    "Add one via POST /config/printers."
                ),
            )
        target = printers.describe_mapping(mapping)

        job_id = uuid.uuid4().hex
        try:
            attempts, served_by = escpos_jobs.run_raw_job(
                mapping, body.data_base64, job_name=f"{JOB_NAME_PREFIX} - {body.printer} (raw)"
            )
        except escpos_jobs.PrintJobError as exc:
            logger.error(
                "print/raw FAILED job=%s logical=%s target=%s attempts=%d error=%s",
                job_id, body.printer, target, exc.attempts, exc,
            )
            _record_job(
                "print/raw", body.printer, ok=False, error=str(exc), job_id=job_id,
                attempts=exc.attempts,
            )
            raise HTTPException(status_code=502, detail=str(exc))

        logger.info(
            "print/raw OK job=%s logical=%s target=%s served_by=%s bytes=%d attempts=%d",
            job_id, body.printer, target, served_by, len(body.data_base64), attempts,
        )
        _record_job(
            "print/raw", body.printer, ok=True, job_id=job_id, attempts=attempts, served_by=served_by
        )
        return {"ok": True, "job_id": job_id}

    @app.post("/print/test", dependencies=[Depends(require_token), Depends(enforce_rate_limit)])
    def print_test(body: PrintTestRequest) -> dict[str, Any]:
        """The single "Test print" action: a connectivity header, a
        horizontal rule, and the bilingual EN/AR test quotes, printed
        together as one job, plus a QR code. One-click way to confirm both
        "does this mapping print at all" and "does non-Latin text render
        correctly" - the latter being the part most likely to silently
        break on a printer/codepage the rest of the API can't detect ahead
        of time. See app/test_print.py and
        escpos_jobs.run_test_print_job.
        """
        cfg = config_mod.load_config()
        try:
            mapping = printers.resolve_printer_settings(body.printer, cfg)
        except printers.UnknownLogicalPrinterError:
            logger.warning("print/test: unknown logical printer '%s'", body.printer)
            _record_job("print/test", body.printer, ok=False, error="unknown logical printer")
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No printer configured with logical name '{body.printer}'. "
                    "Add one via POST /config/printers."
                ),
            )
        target = printers.describe_mapping(mapping)

        job_id = uuid.uuid4().hex
        try:
            image = test_print.build_test_print_image(
                width_px=body.width_px, printer_label=body.printer
            )
        except test_print.FontNotFoundError as exc:
            _record_job("print/test", body.printer, ok=False, error=str(exc), job_id=job_id)
            raise HTTPException(status_code=500, detail=str(exc))

        try:
            attempts, served_by = escpos_jobs.run_test_print_job(
                mapping,
                image,
                job_name=f"{JOB_NAME_PREFIX} - {body.printer} (test print)",
                qr_data=TEST_PRINT_QR_DATA,
                cut=body.cut,
            )
        except escpos_jobs.PrintJobError as exc:
            logger.error(
                "print/test FAILED job=%s logical=%s target=%s attempts=%d error=%s",
                job_id, body.printer, target, exc.attempts, exc,
            )
            _record_job(
                "print/test", body.printer, ok=False, error=str(exc), job_id=job_id,
                attempts=exc.attempts,
            )
            raise HTTPException(status_code=502, detail=str(exc))

        logger.info(
            "print/test OK job=%s logical=%s target=%s served_by=%s width_px=%d attempts=%d",
            job_id, body.printer, target, served_by, body.width_px, attempts,
        )
        _record_job(
            "print/test", body.printer, ok=True, job_id=job_id, attempts=attempts, served_by=served_by
        )
        return {"ok": True, "job_id": job_id}

    @app.post("/print/pdf", dependencies=[Depends(require_token), Depends(enforce_rate_limit)])
    async def print_pdf(
        file: UploadFile = File(..., description="The PDF to print."),
        printer: str = Form(..., description="Logical printer name."),
        dpi: Optional[int] = Form(
            default=None,
            description="Override the printer's configured DPI for this job only.",
        ),
        cut_between_pages: bool = Form(
            default=True, description="Cut the paper between pages (the last page is always cut)."
        ),
        dry_run: bool = Form(
            default=False,
            description="If true, only rasterize and return preview images - never touches the printer.",
        ),
    ) -> dict[str, Any]:
        """Rasterize an uploaded PDF and print it, one bitmap per page.

        multipart/form-data (not JSON/base64) - PDFs can be non-trivial in
        size and multipart avoids ~33% base64 inflation and is what the
        browser's `FormData`/`fetch` produces natively for a file input.

        See app/pdf_jobs.py for why this rasterizes the PDF rather than
        trying to translate its content into ESC/POS text commands, and
        app.escpos_jobs.run_pdf_job for why it shares its raster-sending
        code with the EN/AR test quote.

        `dry_run: true` returns the rasterized page images (the same ones
        that would otherwise be sent to the printer) without ever calling
        run_pdf_job - rasterization already happens unconditionally, so
        this is nearly free. Not logged as a job, since no print was
        actually attempted.
        """
        cfg = config_mod.load_config()
        try:
            mapping = printers.resolve_printer_settings(printer, cfg)
        except printers.UnknownLogicalPrinterError:
            logger.warning("print/pdf: unknown logical printer '%s'", printer)
            _record_job("print/pdf", printer, ok=False, error="unknown logical printer")
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No printer configured with logical name '{printer}'. "
                    "Add one via POST /config/printers."
                ),
            )
        target = printers.describe_mapping(mapping)
        effective_dpi = dpi if dpi is not None else mapping.get("dpi", 203)
        width_px = mapping.get("width_px", 384)

        pdf_bytes = await file.read()

        # job_id is only meaningful for a real job attempt (it's what
        # cross-references a GET /logs entry) - generated after the parse
        # step succeeds, and only actually used past the dry_run check
        # below, so a dry-run preview doesn't mint an unused one.
        job_id = uuid.uuid4().hex

        try:
            images = pdf_jobs.rasterize_pdf(pdf_bytes, dpi=effective_dpi, width_px=width_px)
        except pdf_jobs.PdfRenderError as exc:
            logger.warning("print/pdf: could not rasterize upload from '%s': %s", printer, exc)
            _record_job("print/pdf", printer, ok=False, error=str(exc), job_id=job_id)
            raise HTTPException(status_code=400, detail=str(exc))

        if dry_run:
            logger.info(
                "print/pdf DRY-RUN logical=%s target=%s pages=%d dpi=%d width_px=%d",
                printer, target, len(images), effective_dpi, width_px,
            )
            return {
                "ok": True,
                "dry_run": True,
                "preview_images_base64": [_image_to_base64_png(img) for img in images],
            }

        try:
            attempts, served_by = escpos_jobs.run_pdf_job(
                mapping,
                images,
                job_name=f"{JOB_NAME_PREFIX} - {printer} (PDF, {len(images)}p)",
                cut_between_pages=cut_between_pages,
            )
        except escpos_jobs.PrintJobError as exc:
            logger.error(
                "print/pdf FAILED job=%s logical=%s target=%s attempts=%d error=%s",
                job_id, printer, target, exc.attempts, exc,
            )
            _record_job(
                "print/pdf", printer, ok=False, error=str(exc), job_id=job_id,
                attempts=exc.attempts,
            )
            raise HTTPException(status_code=502, detail=str(exc))

        logger.info(
            "print/pdf OK job=%s logical=%s target=%s served_by=%s pages=%d dpi=%d width_px=%d attempts=%d",
            job_id, printer, target, served_by, len(images), effective_dpi, width_px, attempts,
        )
        _record_job(
            "print/pdf", printer, ok=True, job_id=job_id, attempts=attempts, served_by=served_by
        )
        return {"ok": True, "job_id": job_id, "pages_printed": len(images)}

    # --- Job history ---------------------------------------------------------

    @app.get("/logs", dependencies=[Depends(require_token)])
    def get_logs(limit: int = 200) -> dict[str, Any]:
        """Recent print-job attempts (newest first) - powers the Logs page."""
        return {"jobs": job_log.get_recent(limit=limit)}

    # --- Frontend (built Vite/React/shadcn app) ------------------------------
    # Mounted last and at "/" so every API route above is matched first;
    # Starlette tries routes in registration order and a Mount only catches
    # whatever no earlier exact route claimed (e.g. "/", "/assets/*.js").
    # `html=True` makes StaticFiles serve index.html at "/" itself. The
    # frontend has no client-side router (see frontend/src/App.tsx - view
    # switching is in-memory useState, not distinct URLs), so there's no
    # need for an SPA catch-all that serves index.html for arbitrary unknown
    # paths too - an unmatched path 404ing is the correct behavior here.

    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    else:

        @app.get("/", include_in_schema=False)
        def frontend_missing() -> PlainTextResponse:
            """Fallback for a fresh checkout where frontend/dist hasn't been
            built yet - fails obviously instead of a bare 404."""
            return PlainTextResponse(
                "Print Bridge's frontend hasn't been built yet.\n\n"
                "Run this once:\n"
                "  cd frontend\n"
                "  npm install\n"
                "  npm run build\n\n"
                "Then restart Print Bridge.",
                status_code=500,
            )

    return app


# Module-level app for `uvicorn app.main:app` during development.
app = create_app()


# ---------------------------------------------------------------------------
# Threaded server lifecycle, controlled by the tray icon
# ---------------------------------------------------------------------------


class BridgeServer:
    """Runs the FastAPI app under uvicorn in a background thread.

    uvicorn.Server is designed to be embedded this way: `capture_signals()`
    is a no-op off the main thread, and `should_exit` is polled by the
    server's main loop roughly every 100ms, so setting it from another
    thread is the documented way to stop it.
    """

    def __init__(self) -> None:
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    @property
    def is_running(self) -> bool:
        """True if the server thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Build the app from the current config and start serving on a
        background thread. No-op if already running. Raises RuntimeError if
        the server doesn't come up within 10s (e.g. the port is in use)."""
        if self.is_running:
            return
        cfg = config_mod.load_config()
        self.port = cfg["port"]
        fastapi_app = create_app()
        uv_config = uvicorn.Config(
            fastapi_app,
            host="127.0.0.1",  # never 0.0.0.0 - this must never be LAN-reachable
            port=self.port,
            log_level="warning",
            access_log=False,
            # We already configure our own rotating-file logger (setup_logging());
            # uvicorn's default log_config uses logging.config.dictConfig with a
            # dotted class path for its formatter, which fails to resolve inside
            # a PyInstaller-frozen build ("Unable to configure formatter
            # 'default'"). Disabling it avoids that and avoids uvicorn fighting
            # over the root logger's handlers.
            log_config=None,
        )
        server = uvicorn.Server(uv_config)
        self._server = server
        thread = threading.Thread(target=server.run, name="print-bridge-http", daemon=True)
        self._thread = thread
        thread.start()

        deadline = time.time() + 10
        while time.time() < deadline:
            if server.started:
                logger.info("Print Bridge HTTP server listening on 127.0.0.1:%d", self.port)
                return
            if not thread.is_alive():
                raise RuntimeError(
                    f"Print Bridge failed to start on port {self.port} "
                    "(it may already be in use). See the log file for details."
                )
            time.sleep(0.05)
        raise RuntimeError("Print Bridge HTTP server did not start within 10 seconds.")

    def stop(self) -> None:
        """Signal the server to exit and wait (up to 10s) for its thread to
        finish. Safe to call even if not running."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._server = None
        self._thread = None

    def restart(self) -> None:
        """Stop then start - rebuilds the app from config.json, so this is
        how port/allowed_origins changes made via the config UI take
        effect (tray menu's "Restart server")."""
        logger.info("Restarting Print Bridge HTTP server...")
        self.stop()
        self.start()
