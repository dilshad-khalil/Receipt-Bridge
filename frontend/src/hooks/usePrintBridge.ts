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
import type { ConfigResponse, HealthResponse, JobLogEntry, WindowsPrinter } from "@/lib/types"

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
