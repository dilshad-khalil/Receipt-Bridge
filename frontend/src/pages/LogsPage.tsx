/**
 * Logs page: recent print-job history from `GET /logs` (backed by
 * app/job_log.py's SQLite table) - timestamp, printer, endpoint,
 * success/fail, and the error message on failure. There's no live
 * push/websocket, so "Refresh" is a manual re-fetch.
 */
import { RefreshCw } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useLogs } from "@/hooks/usePrintBridge"
import type { JobLogEntry } from "@/lib/types"

const ENDPOINT_LABEL: Record<string, string> = {
  "print/text": "/print/text",
  "print/raw": "/print/raw",
  "print/pdf": "/print/pdf",
  "print/test": "/print/test",
}

function formatTimestamp(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  })
}

function StatusBadge({ ok }: { ok: boolean }) {
  return ok ? (
    <Badge className="bg-emerald-500/15 text-emerald-400">Success</Badge>
  ) : (
    <Badge className="bg-red-500/15 text-red-400">Failed</Badge>
  )
}

/** Attempt count, only called out when a retry actually happened (see
 * app/escpos_jobs.py's retry-with-backoff) - a plain "1" would just be
 * visual noise for the overwhelmingly common case of a job that worked
 * first try, but a printer that needed retries (or exhausted them and
 * still failed) is exactly the kind of flaky behavior this column exists
 * to surface. */
function AttemptsCell({ attempts }: { attempts: number }) {
  if (attempts <= 1) {
    return <span className="text-muted-foreground">1</span>
  }
  return (
    <Badge variant="outline" className="font-normal text-muted-foreground">
      {attempts} attempts
    </Badge>
  )
}

/** Which configured target actually completed the job (see
 * app/printers.py's describe_target) - only meaningfully different from
 * the "Printer" column when a mapping has failover backups and the
 * primary was down, so an em-dash is shown rather than repeating the
 * logical name for the common single-target case. */
function ServedByCell({ job }: { job: JobLogEntry }) {
  if (!job.served_by) {
    return <span className="text-muted-foreground">-</span>
  }
  return <span className="text-muted-foreground">{job.served_by}</span>
}

function LogRow({ job }: { job: JobLogEntry }) {
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap text-muted-foreground">
        {formatTimestamp(job.timestamp)}
      </TableCell>
      <TableCell>
        <code className="text-xs">{ENDPOINT_LABEL[job.endpoint] ?? job.endpoint}</code>
      </TableCell>
      <TableCell>{job.printer ?? <span className="text-muted-foreground">-</span>}</TableCell>
      <TableCell>
        <ServedByCell job={job} />
      </TableCell>
      <TableCell>
        <StatusBadge ok={job.ok} />
      </TableCell>
      <TableCell>
        <AttemptsCell attempts={job.attempts} />
      </TableCell>
      <TableCell className="max-w-xs truncate text-muted-foreground" title={job.error ?? ""}>
        {job.error ?? ""}
      </TableCell>
    </TableRow>
  )
}

export function LogsPage() {
  const { data: jobs, loading, error, refresh } = useLogs()

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="font-heading text-lg font-semibold">Logs</h1>
          <p className="text-sm text-muted-foreground">
            The last {jobs?.length ?? 200} print attempts, newest first.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={loading ? "animate-spin" : ""} />
          Refresh
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Recent print jobs</CardTitle>
        </CardHeader>
        <CardContent>
          {loading && !jobs && <p className="text-sm text-muted-foreground">Loading...</p>}
          {error && <p className="text-sm text-red-400">Could not load logs: {error}</p>}
          {jobs && jobs.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No print jobs yet - send one from the Printers page.
            </p>
          )}
          {jobs && jobs.length > 0 && (
            // Scrolls internally (not the whole page) once the log list
            // outgrows this height - the page header/Refresh button above
            // stay put while you scroll through history.
            <ScrollArea className="h-[65vh]">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>Endpoint</TableHead>
                    <TableHead>Printer</TableHead>
                    <TableHead>Served by</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Attempts</TableHead>
                    <TableHead>Error</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.map((job, i) => (
                    // job_id can be null (e.g. an unknown-printer failure never
                    // reaches job creation) so index is folded into the key to
                    // keep rows unique.
                    <LogRow key={`${job.job_id ?? "no-id"}-${i}`} job={job} />
                  ))}
                </TableBody>
              </Table>
            </ScrollArea>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
