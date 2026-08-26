/**
 * Printers page (default view): configured logical printer mappings (each
 * pointing at either an installed Windows printer or a raw network ESC/POS
 * printer), a form to add new ones, a live status indicator per mapping,
 * and per-mapping test actions (the combined connectivity + bilingual
 * EN/AR test print, and ad-hoc PDF printing) plus removal.
 */
import { useRef, useState } from "react"
import { toast } from "sonner"
import { Circle, Eye, FileUp, ListOrdered, Loader2, MoreVertical, Printer, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { PreviewDialog } from "@/components/PreviewDialog"
import { TargetsDialog } from "@/components/TargetsDialog"
import { api, ApiError } from "@/lib/api"
import { useConfig, usePrinterStatus, useWindowsPrinters } from "@/hooks/usePrintBridge"
import type { LineItem, PrinterEntry, PrinterStatus } from "@/lib/types"

function errMsg(err: unknown): string {
  return err instanceof ApiError || err instanceof Error ? err.message : String(err)
}

// A small mixed English/Arabic sample receipt, used only by the "Preview"
// button's dry-run /print/text call - demonstrates the same per-line
// Arabic shaping/reordering a real receipt would get (see
// app/escpos_jobs.py's per-line routing and app/text_render.py), without
// pretending to be real transaction data.
const PREVIEW_DEMO_LINES: LineItem[] = [
  { text: "PREVIEW", align: "center", bold: true, width: 2, height: 2 },
  { text: "Sample line - normal text renders like this", align: "left" },
  { text: "معاينة - هكذا يظهر النص العربي على الإيصال", align: "center" },
]

/** Where a mapping actually points, for display in the mappings table -
 * the primary target's label, plus a "+N backup" suffix if the mapping
 * has failover targets configured (see the Targets column's "Manage
 * targets" dialog for the full ordered list). */
function targetLabel(entry: PrinterEntry): string {
  const targets = entry.targets ?? []
  if (targets.length === 0) return "(no targets)"
  const primary = targets[0]
  const primaryLabel = primary.type === "network" ? `${primary.host}:${primary.port}` : primary.name
  return targets.length > 1 ? `${primaryLabel} (+${targets.length - 1} backup)` : primaryLabel
}

const STATUS_LABEL: Record<PrinterStatus, string> = {
  ready: "Ready",
  offline: "Offline",
  paper_out: "Paper out",
  paper_jam: "Paper jam",
  door_open: "Door open",
  paused: "Paused",
  busy: "Busy",
  error: "Error",
  reachable: "Reachable",
  unreachable: "Unreachable",
  unknown: "Unknown",
}
const HEALTHY_STATUSES = new Set<PrinterStatus>(["ready", "reachable", "busy"])

/** Per-row status indicator. Grayscale only, per the design system - a
 * filled dot for a healthy printer, an outlined/dimmed dot for anything
 * that needs attention, distinguished by fill/opacity rather than color. */
function StatusDot({ status }: { status: PrinterStatus | null }) {
  const known = status ?? "unknown"
  const healthy = HEALTHY_STATUSES.has(known)
  return (
    <Badge variant="outline" className="font-normal text-muted-foreground">
      <Circle className={healthy ? "fill-foreground stroke-none" : "fill-none opacity-50"} />
      {STATUS_LABEL[known]}
    </Badge>
  )
}

/** One row of the mappings table: status, and a single actions menu
 * (test print/preview/PDF/manage targets/remove) for a single logical
 * printer. Its own component so each row can track its own
 * "busy"/dialog-open/status-polling state independently. */
function PrinterRow({
  logicalName,
  entry,
  onChanged,
}: {
  logicalName: string
  entry: PrinterEntry
  onChanged: () => void
}) {
  const [busy, setBusy] = useState<"" | "test" | "preview" | "pdf" | "preview-pdf" | "remove">("")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [targetsOpen, setTargetsOpen] = useState(false)
  const [preview, setPreview] = useState<{ title: string; description: string; images: string[] } | null>(
    null,
  )
  const fileInputRef = useRef<HTMLInputElement>(null)
  // Which action the (single, shared) hidden file input's change event
  // should perform - set right before opening the picker, read once a
  // file is actually chosen. Reusing one input for both "Print PDF" and
  // "Preview PDF" avoids a second near-identical file-input element.
  const pdfIntentRef = useRef<"print" | "preview">("print")
  const { status } = usePrinterStatus(logicalName, entry.status)

  // A single combined test: connectivity header + a horizontal rule + the
  // bilingual EN/AR test quotes, all in one job (see app/test_print.py and
  // POST /print/test) - one button, not a separate "connectivity" test and
  // a separate "EN/AR quote" test.
  const runTestPrint = async () => {
    setBusy("test")
    try {
      await api.printTest(logicalName, entry.width_px)
      toast.success(`Test print sent to "${logicalName}"`)
    } catch (err) {
      toast.error(`Test print failed: ${errMsg(err)}`)
    } finally {
      setBusy("")
    }
  }

  // Dry-run preview of a small demo /print/text job (see POST /print/text's
  // dry_run field) - shows a mixed English/Arabic sample receipt rendered
  // at this mapping's configured width, without printing anything. Distinct
  // from "Test print": that one really prints, over on the dedicated
  // /print/test endpoint; this one never touches the printer.
  const runPreview = async () => {
    setBusy("preview")
    try {
      const res = await api.previewText(logicalName, PREVIEW_DEMO_LINES)
      setPreview({
        title: `Preview - "${logicalName}"`,
        description: `A sample receipt rendered at ${entry.width_px}px wide - nothing was printed.`,
        images: res.preview_images_base64,
      })
    } catch (err) {
      toast.error(`Preview failed: ${errMsg(err)}`)
    } finally {
      setBusy("")
    }
  }

  const handlePdfChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = "" // allow re-selecting the same file next time
    if (!file) return

    if (pdfIntentRef.current === "preview") {
      setBusy("preview-pdf")
      try {
        const res = await api.previewPdf(logicalName, file)
        setPreview({
          title: `Preview - "${file.name}"`,
          description: `Rendered as it would print to "${logicalName}" - nothing was printed.`,
          images: res.preview_images_base64,
        })
      } catch (err) {
        toast.error(`PDF preview failed: ${errMsg(err)}`)
      } finally {
        setBusy("")
      }
      return
    }

    setBusy("pdf")
    try {
      const res = await api.printPdf(logicalName, file)
      toast.success(`Printed ${res.pages_printed} page(s) of "${file.name}" to "${logicalName}"`)
    } catch (err) {
      toast.error(`PDF print failed: ${errMsg(err)}`)
    } finally {
      setBusy("")
    }
  }

  const choosePdf = (intent: "print" | "preview") => {
    pdfIntentRef.current = intent
    fileInputRef.current?.click()
  }

  return (
    <TableRow>
      <TableCell className="font-medium">{logicalName}</TableCell>
      <TableCell className="text-muted-foreground">{targetLabel(entry)}</TableCell>
      <TableCell>
        <StatusDot status={status} />
      </TableCell>
      <TableCell className="text-muted-foreground">{entry.dpi}</TableCell>
      <TableCell className="text-muted-foreground">{entry.width_px}px</TableCell>
      <TableCell>
        <div className="flex justify-end">
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            onChange={handlePdfChosen}
          />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="icon-sm"
                disabled={busy !== ""}
                aria-label={`Actions for "${logicalName}"`}
              >
                {busy !== "" ? <Loader2 className="animate-spin" /> : <MoreVertical />}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              <DropdownMenuItem
                onSelect={runTestPrint}
                title="Prints a connectivity check plus bilingual English/Arabic test quotes, so shaping/RTL rendering is confirmed too"
              >
                <Printer />
                Test print
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={runPreview}
                title="Renders a sample English/Arabic receipt at this printer's configured width - nothing is printed"
              >
                <Eye />
                Preview
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => choosePdf("print")}>
                <FileUp />
                Print PDF
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => choosePdf("preview")}
                title="Rasterizes the PDF and shows the pages as they'd print - nothing is printed"
              >
                <Eye />
                Preview PDF
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() => setTargetsOpen(true)}
                title="Add/remove/reorder failover backup targets for this mapping"
              >
                <ListOrdered />
                Manage targets
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => setConfirmOpen(true)}>
                <Trash2 />
                Remove
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {preview && (
          <PreviewDialog
            open={preview !== null}
            onOpenChange={(open) => {
              if (!open) setPreview(null)
            }}
            title={preview.title}
            description={preview.description}
            images={preview.images}
          />
        )}
        <TargetsDialog
          open={targetsOpen}
          onOpenChange={setTargetsOpen}
          logicalName={logicalName}
          targets={entry.targets ?? []}
          onSaved={onChanged}
        />
        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title={`Remove "${logicalName}"?`}
          description={`This deletes the mapping to "${targetLabel(entry)}". Any webpage still sending print jobs to "${logicalName}" will start getting 404s until it's re-added.`}
          onConfirm={async () => {
            try {
              await api.deletePrinter(logicalName)
              toast.success(`Removed mapping "${logicalName}"`)
              onChanged()
            } catch (err) {
              toast.error(`Could not remove mapping: ${errMsg(err)}`)
              throw err
            }
          }}
        />
      </TableCell>
    </TableRow>
  )
}

/** Form to add (or re-point) a logical printer mapping. A Tabs toggle picks
 * the target type - an installed Windows printer (via Select) or a raw
 * network printer (via Host/Port Inputs), needing no Windows driver at
 * all. DPI/width are optional - left blank, a new mapping gets the module
 * defaults (203dpi/384px) and an existing one keeps whatever it already had
 * (see config.upsert_printer). */
function AddMappingForm({ onAdded }: { onAdded: () => void }) {
  const { data: windowsPrinters, loading: printersLoading } = useWindowsPrinters()
  const [targetType, setTargetType] = useState<"windows" | "network">("windows")
  const [logicalName, setLogicalName] = useState("")
  const [windowsPrinter, setWindowsPrinter] = useState("")
  const [host, setHost] = useState("")
  const [port, setPort] = useState("9100")
  const [dpi, setDpi] = useState("")
  const [widthPx, setWidthPx] = useState("")
  const [saving, setSaving] = useState(false)

  const canSave =
    logicalName.trim() !== "" &&
    !saving &&
    (targetType === "windows" ? windowsPrinter !== "" : host.trim() !== "")

  const handleSave = async () => {
    if (!canSave) {
      if (!logicalName.trim()) toast.error("Enter a logical name first")
      else if (targetType === "windows") toast.error("Choose a Windows printer")
      else toast.error("Enter a host/IP for the network printer")
      return
    }
    setSaving(true)
    try {
      await api.upsertPrinter({
        logical_name: logicalName.trim(),
        type: targetType,
        windows_printer_name: targetType === "windows" ? windowsPrinter : undefined,
        host: targetType === "network" ? host.trim() : undefined,
        port: targetType === "network" && port ? Number(port) : undefined,
        dpi: dpi ? Number(dpi) : null,
        width_px: widthPx ? Number(widthPx) : null,
      })
      const target = targetType === "windows" ? windowsPrinter : `${host.trim()}:${port || "9100"}`
      toast.success(`Mapped "${logicalName.trim()}" -> "${target}"`)
      setLogicalName("")
      setHost("")
      setDpi("")
      setWidthPx("")
      onAdded()
    } catch (err) {
      toast.error(`Could not save mapping: ${errMsg(err)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Tabs value={targetType} onValueChange={(v) => setTargetType(v as "windows" | "network")}>
        <TabsList>
          <TabsTrigger value="windows">Windows printer</TabsTrigger>
          <TabsTrigger value="network">Network printer</TabsTrigger>
        </TabsList>
      </Tabs>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="col-span-2 sm:col-span-1">
          <Label htmlFor="logical-name" className="mb-1.5">
            Logical name
          </Label>
          <Input
            id="logical-name"
            placeholder="e.g. receipt_printer_1"
            value={logicalName}
            onChange={(e) => setLogicalName(e.target.value)}
          />
        </div>

        {targetType === "windows" ? (
          <div className="col-span-2 sm:col-span-1">
            <Label htmlFor="windows-printer" className="mb-1.5">
              Windows printer
            </Label>
            <Select value={windowsPrinter} onValueChange={setWindowsPrinter}>
              <SelectTrigger id="windows-printer" className="w-full">
                <SelectValue placeholder={printersLoading ? "Loading..." : "Select a printer"} />
              </SelectTrigger>
              <SelectContent>
                {(windowsPrinters ?? []).map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name}
                    {p.is_default ? " (Windows default)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <>
            <div>
              <Label htmlFor="host" className="mb-1.5">
                Host / IP
              </Label>
              <Input
                id="host"
                placeholder="192.168.1.50"
                value={host}
                onChange={(e) => setHost(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="net-port" className="mb-1.5">
                Port
              </Label>
              <Input
                id="net-port"
                type="number"
                placeholder="9100"
                value={port}
                onChange={(e) => setPort(e.target.value)}
              />
            </div>
          </>
        )}

        <div>
          <Label htmlFor="dpi" className="mb-1.5">
            DPI
          </Label>
          <Input
            id="dpi"
            type="number"
            placeholder="203"
            value={dpi}
            onChange={(e) => setDpi(e.target.value)}
          />
        </div>
        <div>
          <Label htmlFor="width-px" className="mb-1.5">
            Width (px)
          </Label>
          <Input
            id="width-px"
            type="number"
            placeholder="384"
            value={widthPx}
            onChange={(e) => setWidthPx(e.target.value)}
          />
        </div>
        <div className="col-span-2 sm:col-span-4">
          <p className="mb-2 text-xs text-muted-foreground">
            DPI and width only matter for PDF printing (<code>POST /print/pdf</code>) - leave
            blank to use the defaults (203 DPI, 384px &asymp; 58mm; use 576px for an 80mm printer).
            {targetType === "network" && (
              <>
                {" "}
                A network printer needs no Windows driver - just its IP and ESC/POS port (usually
                9100).
              </>
            )}
          </p>
          <Button onClick={handleSave} disabled={!canSave}>
            {saving && <Loader2 className="animate-spin" />}
            Save mapping
          </Button>
        </div>
      </div>
    </div>
  )
}

export function PrintersPage() {
  const { data: config, loading, error, refresh } = useConfig()
  const entries = Object.entries(config?.printers ?? {})

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-heading text-lg font-semibold">Printers</h1>
        <p className="text-sm text-muted-foreground">
          Map a logical name (what your webpage calls) to a real printer - an installed Windows
          printer, or a network printer reachable directly over TCP.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Add mapping</CardTitle>
          <CardDescription>
            Windows printers come from <code>GET /printers</code> - anything installed and visible
            to this Windows user. Network printers are entered by hand (they aren't
            auto-discovered).
          </CardDescription>
        </CardHeader>
        <CardContent>
          <AddMappingForm onAdded={refresh} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Configured mappings</CardTitle>
        </CardHeader>
        <CardContent>
          {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
          {error && <p className="text-sm text-red-400">Could not load config: {error}</p>}
          {!loading && !error && entries.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No mappings yet - add one above to get started.
            </p>
          )}
          {entries.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Logical name</TableHead>
                  <TableHead>Target</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>DPI</TableHead>
                  <TableHead>Width</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {entries.map(([logical, entry]) => (
                  <PrinterRow key={logical} logicalName={logical} entry={entry} onChanged={refresh} />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
