/**
 * Thin fetch wrapper for the Print Bridge HTTP API.
 *
 * The frontend is served BY the same FastAPI process it talks to (see
 * app/main.py's StaticFiles mount), so every request below is a same-origin
 * relative path ("/health", "/print/pdf", ...) - no base URL to configure,
 * and no CORS/Private-Network-Access preflight to worry about from this
 * page itself (that machinery in app/main.py exists for *other* origins,
 * e.g. a real point-of-sale webpage calling the bridge from its own site).
 *
 * Auth: if the operator has set an auth token (Settings page), every
 * /print/* and /config/* request needs an `X-Print-Bridge-Token` header.
 * The token is kept in localStorage so it survives a page reload.
 */
import type {
  ConfigResponse,
  CutMode,
  HealthResponse,
  JobLogEntry,
  LineItem,
  PrinterMappingRequest,
  PrinterStatus,
  PrintJobResponse,
  PrintPdfResponse,
  PrintPreviewResponse,
  SettingsResponse,
  SettingsUpdateRequest,
  TargetSpecRequest,
  WindowsPrinter,
} from "./types"

const TOKEN_KEY = "printBridgeToken"

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ""
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/** Thrown for any non-2xx response; `.message` is the server's `detail`
 * field when present (FastAPI's HTTPException shape), else the HTTP status
 * text - so callers can show it directly in a toast. */
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  const token = getToken()
  if (token) headers.set("X-Print-Bridge-Token", token)
  // FormData sets its own multipart Content-Type (with boundary) - only
  // force JSON when the body is a plain string we constructed ourselves.
  if (options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json")
  }

  const res = await fetch(path, { ...options, headers })

  const contentType = res.headers.get("content-type") ?? ""
  const data = contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : null

  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : res.statusText || `HTTP ${res.status}`
    throw new ApiError(res.status, detail)
  }
  return data as T
}

const json = (body: unknown) => JSON.stringify(body)

export const api = {
  health: () => request<HealthResponse>("/health"),
  windowsPrinters: () => request<{ printers: WindowsPrinter[] }>("/printers"),
  config: () => request<ConfigResponse>("/config"),

  upsertPrinter: (body: PrinterMappingRequest) =>
    request<{ ok: true; printers: ConfigResponse["printers"] }>("/config/printers", {
      method: "POST",
      body: json(body),
    }),

  /** Live status for one mapping - see app/printers.py's get_printer_status.
   * Used by the Printers page's per-row status indicator, polled
   * periodically (see hooks/usePrintBridge.ts's usePrinterStatus). */
  printerStatus: (logicalName: string) =>
    request<{ logical_name: string; status: PrinterStatus }>(
      `/config/printers/${encodeURIComponent(logicalName)}/status`,
    ),

  /** Replace an existing mapping's ordered target list wholesale - the
   * Printers page's target-editor dialog calls this on every
   * add/remove/reorder, resubmitting the complete list rather than a
   * single incremental change (see POST /config/printers/{name}/targets).
   * Index 0 is the primary, tried first; the rest are failover backups. */
  setPrinterTargets: (logicalName: string, targets: TargetSpecRequest[]) =>
    request<{ ok: true; printers: ConfigResponse["printers"] }>(
      `/config/printers/${encodeURIComponent(logicalName)}/targets`,
      { method: "POST", body: json({ targets }) },
    ),

  deletePrinter: (logicalName: string) =>
    request<{ ok: true; printers: ConfigResponse["printers"] }>(
      `/config/printers/${encodeURIComponent(logicalName)}`,
      { method: "DELETE" },
    ),

  updateSettings: (body: SettingsUpdateRequest) =>
    request<SettingsResponse>("/config/settings", { method: "POST", body: json(body) }),

  setStartupEnabled: (enabled: boolean) =>
    request<{ ok: true; startup_enabled: boolean }>("/config/startup", {
      method: "POST",
      body: json({ enabled }),
    }),

  /** The single "Test print" action - a connectivity header, a horizontal
   * rule, and the bilingual EN/AR test quotes, printed together as one job
   * plus a QR code (see app/test_print.py and POST /print/test). */
  printTest: (logicalName: string, widthPx = 384) =>
    request<PrintJobResponse>("/print/test", {
      method: "POST",
      body: json({ printer: logicalName, width_px: widthPx, cut: true }),
    }),

  printPdf: (logicalName: string, file: File, cutBetweenPages = true) => {
    const form = new FormData()
    form.append("printer", logicalName)
    form.append("file", file)
    form.append("cut_between_pages", String(cutBetweenPages))
    return request<PrintPdfResponse>("/print/pdf", { method: "POST", body: form })
  },

  /** Dry-run preview of a `/print/text` job - renders `lines` (plus an
   * optional cut mode, purely for parity with a real request) to an image
   * without ever touching the printer (see POST /print/text's `dry_run`
   * field and app/text_render.py's render_lines_preview). */
  previewText: (logicalName: string, lines: LineItem[], cut: CutMode = "none") =>
    request<PrintPreviewResponse>("/print/text", {
      method: "POST",
      body: json({ printer: logicalName, lines, cut, dry_run: true }),
    }),

  /** Dry-run preview of a `/print/pdf` job - rasterizes the PDF and
   * returns its page images without printing (see POST /print/pdf's
   * `dry_run` field). Nearly free: rasterization already has to happen
   * either way. */
  previewPdf: (logicalName: string, file: File) => {
    const form = new FormData()
    form.append("printer", logicalName)
    form.append("file", file)
    form.append("dry_run", "true")
    return request<PrintPreviewResponse>("/print/pdf", { method: "POST", body: form })
  },

  logs: (limit = 200) => request<{ jobs: JobLogEntry[] }>(`/logs?limit=${limit}`),
}
