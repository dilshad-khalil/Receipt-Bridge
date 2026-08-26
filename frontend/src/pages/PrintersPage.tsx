/**
 * Printers page (default view): configured logical->Windows printer
 * mappings, a form to add new ones, and per-mapping test actions (the
 * combined connectivity + bilingual EN/AR test print, and ad-hoc PDF
 * printing) plus removal.
 */
import { useRef, useState } from "react"
import { toast } from "sonner"
import { FileUp, Loader2, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { api, ApiError } from "@/lib/api"
import { useConfig, useWindowsPrinters } from "@/hooks/usePrintBridge"

function errMsg(err: unknown): string {
  return err instanceof ApiError || err instanceof Error ? err.message : String(err)
}

/** One row of the mappings table: test-print/PDF/remove actions for a
 * single logical printer. Its own component so each row can track its own
 * "busy" / dialog-open state independently. */
function PrinterRow({
  logicalName,
  windowsPrinterName,
  dpi,
  widthPx,
  onRemoved,
}: {
  logicalName: string
  windowsPrinterName: string
  dpi: number
  widthPx: number
  onRemoved: () => void
}) {
  const [busy, setBusy] = useState<"" | "test" | "pdf" | "remove">("")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // A single combined test: connectivity header + a horizontal rule + the
  // bilingual EN/AR test quotes, all in one job (see app/test_print.py and
  // POST /print/test) - one button, not a separate "connectivity" test and
  // a separate "EN/AR quote" test.
  const runTestPrint = async () => {
    setBusy("test")
    try {
      await api.printTest(logicalName, widthPx)
      toast.success(`Test print sent to "${logicalName}"`)
    } catch (err) {
      toast.error(`Test print failed: ${errMsg(err)}`)
    } finally {
      setBusy("")
    }
  }

  const handlePdfChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = "" // allow re-selecting the same file next time
    if (!file) return
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

  return (
    <TableRow>
      <TableCell className="font-medium">{logicalName}</TableCell>
      <TableCell className="text-muted-foreground">{windowsPrinterName}</TableCell>
      <TableCell className="text-muted-foreground">{dpi}</TableCell>
      <TableCell className="text-muted-foreground">{widthPx}px</TableCell>
      <TableCell>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Button
            variant="outline"
            size="sm"
            onClick={runTestPrint}
            disabled={busy !== ""}
            title="Prints a connectivity check plus bilingual English/Arabic test quotes, so shaping/RTL rendering is confirmed too"
          >
            {busy === "test" && <Loader2 className="animate-spin" />}
            Test print
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            onChange={handlePdfChosen}
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy !== ""}
          >
            {busy === "pdf" ? <Loader2 className="animate-spin" /> : <FileUp />}
            Print PDF
          </Button>
          <Button
            variant="destructive"
            size="icon-sm"
            onClick={() => setConfirmOpen(true)}
            disabled={busy !== ""}
            aria-label={`Remove mapping "${logicalName}"`}
          >
            <Trash2 />
          </Button>
        </div>
        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title={`Remove "${logicalName}"?`}
          description={`This deletes the mapping to Windows printer "${windowsPrinterName}". Any webpage still sending print jobs to "${logicalName}" will start getting 404s until it's re-added.`}
          onConfirm={async () => {
            try {
              await api.deletePrinter(logicalName)
              toast.success(`Removed mapping "${logicalName}"`)
              onRemoved()
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

/** Form to add (or re-point) a logical->Windows printer mapping. DPI/width
 * are optional - left blank, a new mapping gets the module defaults
 * (203dpi/384px) and an existing one keeps whatever it already had (see
 * config.upsert_printer). */
function AddMappingForm({ onAdded }: { onAdded: () => void }) {
  const { data: windowsPrinters, loading: printersLoading } = useWindowsPrinters()
  const [logicalName, setLogicalName] = useState("")
  const [windowsPrinter, setWindowsPrinter] = useState("")
  const [dpi, setDpi] = useState("")
  const [widthPx, setWidthPx] = useState("")
  const [saving, setSaving] = useState(false)

  const canSave = logicalName.trim() !== "" && windowsPrinter !== "" && !saving

  const handleSave = async () => {
    if (!canSave) {
      if (!logicalName.trim()) toast.error("Enter a logical name first")
      else if (!windowsPrinter) toast.error("Choose a Windows printer")
      return
    }
    setSaving(true)
    try {
      await api.upsertPrinter({
        logical_name: logicalName.trim(),
        windows_printer_name: windowsPrinter,
        dpi: dpi ? Number(dpi) : null,
        width_px: widthPx ? Number(widthPx) : null,
      })
      toast.success(`Mapped "${logicalName.trim()}" -> "${windowsPrinter}"`)
      setLogicalName("")
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
      <div className="col-span-2 sm:col-span-1">
        <Label htmlFor="windows-printer" className="mb-1.5">
          Windows printer
        </Label>
        <Select value={windowsPrinter} onValueChange={setWindowsPrinter}>
          <SelectTrigger id="windows-printer" className="w-full">
            <SelectValue
              placeholder={printersLoading ? "Loading..." : "Select a printer"}
            />
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
        </p>
        <Button onClick={handleSave} disabled={!canSave}>
          {saving && <Loader2 className="animate-spin" />}
          Save mapping
        </Button>
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
          Map a logical name (what your webpage calls) to a real Windows printer.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Add mapping</CardTitle>
          <CardDescription>
            Windows printers come from <code>GET /printers</code> - anything installed and
            visible to this Windows user.
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
                  <TableHead>Windows printer</TableHead>
                  <TableHead>DPI</TableHead>
                  <TableHead>Width</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {entries.map(([logical, entry]) => (
                  <PrinterRow
                    key={logical}
                    logicalName={logical}
                    windowsPrinterName={entry.windows_printer_name}
                    dpi={entry.dpi}
                    widthPx={entry.width_px}
                    onRemoved={refresh}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
