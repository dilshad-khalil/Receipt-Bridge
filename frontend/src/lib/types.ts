/**
 * Shared TypeScript shapes for the Print Bridge HTTP API. Kept in sync by
 * hand with app/main.py's Pydantic models - there's no shared schema
 * generation step, so if you add/rename a field on one side, update the
 * other.
 */

/** One printer Windows itself knows about (`GET /printers`). */
export interface WindowsPrinter {
  name: string
  is_default: boolean
}

/** Live status of one mapping (`GET /config`'s embedded summary, or
 * `GET /config/printers/{name}/status`). Meaning differs by printer type -
 * see app/printers.py's `get_printer_status` - a "windows" mapping reports
 * real spooler status, a "network" mapping is only ever a reachability
 * probe ("reachable"/"unreachable"). */
export type PrinterStatus =
  | "ready"
  | "offline"
  | "paper_out"
  | "paper_jam"
  | "door_open"
  | "paused"
  | "busy"
  | "error"
  | "reachable"
  | "unreachable"
  | "unknown"

/** A single failover target within a mapping's ordered `targets` list - an
 * installed Windows printer, reached via python-escpos's Win32Raw
 * connector. */
export interface WindowsTarget {
  type: "windows"
  name: string
}

/** A single failover target within a mapping's ordered `targets` list - a
 * raw ESC/POS printer over TCP (typically port 9100), no Windows
 * driver/installation needed, reached via python-escpos's Network
 * connector. */
export interface NetworkTarget {
  type: "network"
  host: string
  port: number
}

export type PrinterTarget = WindowsTarget | NetworkTarget

/** A configured logical->printer mapping, as stored in config.json and
 * returned by `GET /config`. `targets` is ordered - index 0 is the
 * primary, tried first; the rest are backups, tried in order only once
 * the ones before them exhaust their own retries (see
 * app/escpos_jobs.py's failover logic). `dpi`/`width_px` are mapping-level
 * (not per-target) and only used by `POST /print/pdf` (see
 * app/pdf_jobs.py) as the default render settings for that printer.
 * `status` (from the primary target only - see app/printers.py's
 * get_printer_status) powers the Printers page's status dot. */
export interface PrinterEntry {
  targets: PrinterTarget[]
  dpi: number
  width_px: number
  status?: PrinterStatus
}

export interface ConfigResponse {
  port: number
  allowed_origins: string[]
  printers: Record<string, PrinterEntry>
  auth_enabled: boolean
  rate_limit_per_minute: number
  startup_enabled: boolean
}

export interface HealthResponse {
  status: string
  version: string
}

export interface SettingsUpdateRequest {
  port?: number
  allowed_origins?: string[]
  auth_token?: string | null
  rate_limit_per_minute?: number
}

export interface SettingsResponse {
  ok: true
  port: number
  allowed_origins: string[]
  auth_enabled: boolean
  rate_limit_per_minute: number
  restart_required: boolean
}

export interface PrinterMappingRequest {
  logical_name: string
  type: "windows" | "network"
  windows_printer_name?: string
  host?: string
  port?: number
  dpi?: number | null
  width_px?: number | null
}

/** One entry of a `POST /config/printers/{logical_name}/targets` request -
 * see app/main.py's TargetSpec. Same per-target shape as
 * PrinterMappingRequest, minus dpi/width_px (mapping-level, not
 * per-target). */
export interface TargetSpecRequest {
  type: "windows" | "network"
  windows_printer_name?: string
  host?: string
  port?: number
}

/** One row from `GET /logs` - one recorded attempt through any /print/*
 * endpoint, success or failure. */
export interface JobLogEntry {
  /** Unix seconds (float, as Python's time.time() produces). */
  timestamp: number
  endpoint: string
  printer: string | null
  ok: boolean
  error: string | null
  job_id: string | null
  /** How many attempts app/escpos_jobs.py's retry loop took - 1 means no
   * retry was needed; higher means a transient failure (spooler busy,
   * printer temporarily offline/unreachable) was retried before the final
   * outcome, success or failure. Sums across every target tried if the
   * mapping has failover backups. */
  attempts: number
  /** Which configured target actually completed the job (see
   * app/printers.py's describe_target) - `null` if the job never reached
   * a target, or every target failed. Differs from `printer` (the
   * logical name) whenever a mapping's primary target was down and a
   * backup served it instead. */
  served_by: string | null
}

export interface PrintJobResponse {
  ok: true
  job_id: string
}

export interface PrintPdfResponse {
  ok: true
  job_id: string
  pages_printed: number
}

/** One line of a `/print/text` job - see app/main.py's LineItem. Used both
 * for a real print and (with `dry_run: true`) for a preview. */
export interface LineItem {
  text: string
  align?: "left" | "center" | "right"
  bold?: boolean
  width?: number
  height?: number
}

/** How a `/print/text` job cuts the paper afterward - the new three-way
 * mode; the server also still accepts the original boolean for backward
 * compatibility, but the frontend always sends the explicit string. */
export type CutMode = "none" | "partial" | "full"

/** Response from `POST /print/text` or `POST /print/pdf` when `dry_run` is
 * true - one base64 PNG per page (a single element for /print/text), and
 * no printer was touched. */
export interface PrintPreviewResponse {
  ok: true
  dry_run: true
  preview_images_base64: string[]
}
