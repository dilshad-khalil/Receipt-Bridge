/**
 * Data-fetching hooks for the Print Bridge API.
 *
 * There's no router and no data-fetching library here on purpose (this is a
 * small local single-purpose tool, not worth the dependency) - each hook is
 * just `useState` + `useEffect` + a manual `refresh()` escape hatch, which
 * pages call after a mutation (adding/removing a mapping, sending a print
 * job) so the UI reflects the change without a full page reload.
 */
import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type {
  ConfigResponse,
  HealthResponse,
  JobLogEntry,
  PrinterStatus,
  WindowsPrinter,
} from "@/lib/types"

interface AsyncState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

function useAsync<T>(fetcher: () => Promise<T>): AsyncState<T> & { refresh: () => void } {
  // The fetcher closure is recreated every render by callers like
  // useLogs(limit) (it closes over `limit`), so it can't be a dependency of
  // `refresh` below without making `refresh` a new function every render -
  // which would re-trigger the mount effect every render too (an infinite
  // fetch loop). Stashing it in a ref lets `refresh` stay referentially
  // stable while still always calling the latest version.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: true, error: null })

  const refresh = useCallback(() => {
    let cancelled = false
    setState((s) => ({ ...s, loading: true, error: null }))
    fetcherRef
      .current()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null })
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ data: null, loading: false, error: err instanceof Error ? err.message : String(err) })
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => refresh(), [refresh])

  return { ...state, refresh }
}

/** Server liveness/version, polled once on mount - drives the header's
 * connection status pill. */
export function useHealth() {
  return useAsync<HealthResponse>(api.health)
}

/** Windows printers installed on this machine (`GET /printers`) - the
 * options list for the "add mapping" Select. */
export function useWindowsPrinters(): AsyncState<WindowsPrinter[]> & { refresh: () => void } {
  const { data, loading, error, refresh } = useAsync<{ printers: WindowsPrinter[] }>(
    api.windowsPrinters,
  )
  return { data: data?.printers ?? null, loading, error, refresh }
}

/** Server config: port, allowed origins, printer mappings, auth/startup
 * state. Call `.refresh()` after any POST/DELETE to /config/* so the UI
 * reflects the change immediately. */
export function useConfig() {
  return useAsync<ConfigResponse>(api.config)
}

/** Recent print-job history (Logs page). */
export function useLogs(limit = 200): AsyncState<JobLogEntry[]> & { refresh: () => void } {
  const { data, loading, error, refresh } = useAsync<{ jobs: JobLogEntry[] }>(() => api.logs(limit))
  return { data: data?.jobs ?? null, loading, error, refresh }
}

/** Live status for one printer mapping, polled every `intervalMs` (default
 * 15s) via `GET /config/printers/{logical_name}/status` - the Printers
 * page's per-row status indicator. Deliberately its own tiny poller per row
 * rather than a page-wide timer that re-fetches all of `GET /config`, so
 * polling cost scales with how many mappings are actually rendered.
 *
 * `initialStatus` (the summary `GET /config` already computed) seeds the
 * first render so the indicator doesn't flash "Unknown" while this hook's
 * own first poll is still in flight. */
export function usePrinterStatus(
  logicalName: string,
  initialStatus?: PrinterStatus,
  intervalMs = 15000,
): { status: PrinterStatus | null; loading: boolean } {
  const [status, setStatus] = useState<PrinterStatus | null>(initialStatus ?? null)
  const [loading, setLoading] = useState(initialStatus === undefined)

  useEffect(() => {
    let cancelled = false
    const poll = () => {
      api
        .printerStatus(logicalName)
        .then((res) => {
          if (!cancelled) {
            setStatus(res.status)
            setLoading(false)
          }
        })
        .catch(() => {
          // A transient error probing status shouldn't wipe out a
          // previously-known-good status - just leave it stale until the
          // next successful poll.
          if (!cancelled) setLoading(false)
        })
    }
    poll()
    const id = setInterval(poll, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [logicalName, intervalMs])

  return { status, loading }
}
