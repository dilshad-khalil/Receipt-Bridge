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

/** A configured logical->Windows printer mapping, as stored in config.json
 * and returned by `GET /config`. `dpi`/`width_px` are only used by
 * `POST /print/pdf` (see app/pdf_jobs.py) as the default render settings
 * for that printer. */
export interface PrinterEntry {
  windows_printer_name: string
  dpi: number
  width_px: number
}

export interface ConfigResponse {
  port: number
  allowed_origins: string[]
  printers: Record<string, PrinterEntry>
  auth_enabled: boolean
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
}

export interface SettingsResponse {
  ok: true
  port: number
  allowed_origins: string[]
  auth_enabled: boolean
  restart_required: boolean
}

export interface PrinterMappingRequest {
  logical_name: string
  windows_printer_name: string
  dpi?: number | null
  width_px?: number | null
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
