/**
 * Target-editor dialog: edits a mapping's ordered `targets` list (see
 * app/config.py's failover schema and app/escpos_jobs.py's failover
 * logic) - add a backup target, remove one, or reorder them. Index 0 is
 * always the primary, tried first; each one after it is only tried once
 * everything above it has exhausted its own retries.
 *
 * Built entirely from primitives already used elsewhere on this page
 * (Dialog, Card, Button, Tabs, Select, Input) - reordering is two plain
 * up/down icon buttons per row, not a drag-and-drop library, matching the
 * existing "no new component style" constraint.
 */
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { ArrowDown, ArrowUp, Loader2, Plus, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { api, ApiError } from "@/lib/api"
import { useWindowsPrinters } from "@/hooks/usePrintBridge"
import type { PrinterTarget, TargetSpecRequest } from "@/lib/types"

function errMsg(err: unknown): string {
  return err instanceof ApiError || err instanceof Error ? err.message : String(err)
}

function targetLabel(t: PrinterTarget): string {
  return t.type === "network" ? `${t.host}:${t.port}` : t.name
}

function toRequest(t: PrinterTarget): TargetSpecRequest {
  return t.type === "network"
    ? { type: "network", host: t.host, port: t.port }
    : { type: "windows", windows_printer_name: t.name }
}

interface TargetsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  logicalName: string
  targets: PrinterTarget[]
  /** Called after a successful save - the caller refreshes GET /config. */
  onSaved: () => void
}

export function TargetsDialog({
  open,
  onOpenChange,
  logicalName,
  targets,
  onSaved,
}: TargetsDialogProps) {
  const { data: windowsPrinters } = useWindowsPrinters()
  const [list, setList] = useState<PrinterTarget[]>(targets)
  const [saving, setSaving] = useState(false)

  const [newType, setNewType] = useState<"windows" | "network">("windows")
  const [newWindowsPrinter, setNewWindowsPrinter] = useState("")
  const [newHost, setNewHost] = useState("")
  const [newPort, setNewPort] = useState("9100")

  // Reset to the mapping's currently-saved targets every time the dialog
  // opens, so a cancelled edit (or a stale prop from before a refresh)
  // never leaks into the next time it's opened.
  useEffect(() => {
    if (open) setList(targets)
  }, [open, targets])

  const move = (index: number, direction: -1 | 1) => {
    setList((prev) => {
      const swapWith = index + direction
      if (swapWith < 0 || swapWith >= prev.length) return prev
      const next = [...prev]
      ;[next[index], next[swapWith]] = [next[swapWith], next[index]]
      return next
    })
  }

  const remove = (index: number) => {
    setList((prev) => prev.filter((_, i) => i !== index))
  }

  const addTarget = () => {
    if (newType === "windows") {
      if (!newWindowsPrinter) {
        toast.error("Choose a Windows printer first")
        return
      }
      setList((prev) => [...prev, { type: "windows", name: newWindowsPrinter }])
      setNewWindowsPrinter("")
    } else {
      if (!newHost.trim()) {
        toast.error("Enter a host/IP first")
        return
      }
      setList((prev) => [
        ...prev,
        { type: "network", host: newHost.trim(), port: Number(newPort) || 9100 },
      ])
      setNewHost("")
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await api.setPrinterTargets(logicalName, list.map(toRequest))
      toast.success(`Updated targets for "${logicalName}"`)
      onSaved()
      onOpenChange(false)
    } catch (err) {
      toast.error(`Could not save targets: ${errMsg(err)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Targets for &quot;{logicalName}&quot;</DialogTitle>
          <DialogDescription>
            Tried in order - the first is primary; each one below it is only tried once every
            target above it has exhausted its own retries.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          {list.map((t, i) => (
            <Card key={i} size="sm">
              <CardContent className="flex items-center justify-between gap-2">
                <div className="flex flex-col">
                  <span className="text-sm font-medium">{i === 0 ? "Primary" : `Backup ${i}`}</span>
                  <span className="text-xs text-muted-foreground">{targetLabel(t)}</span>
                </div>
                <div className="flex gap-1">
                  <Button
                    variant="outline"
                    size="icon-sm"
                    onClick={() => move(i, -1)}
                    disabled={i === 0}
                    aria-label="Move up (higher priority)"
                  >
                    <ArrowUp />
                  </Button>
                  <Button
                    variant="outline"
                    size="icon-sm"
                    onClick={() => move(i, 1)}
                    disabled={i === list.length - 1}
                    aria-label="Move down (lower priority)"
                  >
                    <ArrowDown />
                  </Button>
                  <Button
                    variant="destructive"
                    size="icon-sm"
                    onClick={() => remove(i)}
                    disabled={list.length <= 1}
                    aria-label="Remove target"
                    title={
                      list.length <= 1 ? "A mapping needs at least one target" : "Remove target"
                    }
                  >
                    <Trash2 />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        <Card size="sm">
          <CardContent className="flex flex-col gap-3">
            <Tabs value={newType} onValueChange={(v) => setNewType(v as "windows" | "network")}>
              <TabsList>
                <TabsTrigger value="windows">Windows printer</TabsTrigger>
                <TabsTrigger value="network">Network printer</TabsTrigger>
              </TabsList>
            </Tabs>
            {newType === "windows" ? (
              <div>
                <Label className="mb-1.5">Windows printer</Label>
                <Select value={newWindowsPrinter} onValueChange={setNewWindowsPrinter}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select a printer" />
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
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <Label className="mb-1.5">Host / IP</Label>
                  <Input
                    placeholder="192.168.1.51"
                    value={newHost}
                    onChange={(e) => setNewHost(e.target.value)}
                  />
                </div>
                <div>
                  <Label className="mb-1.5">Port</Label>
                  <Input
                    type="number"
                    placeholder="9100"
                    value={newPort}
                    onChange={(e) => setNewPort(e.target.value)}
                  />
                </div>
              </div>
            )}
            <Button variant="outline" size="sm" onClick={addTarget} className="self-start">
              <Plus />
              Add as backup target
            </Button>
          </CardContent>
        </Card>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving && <Loader2 className="animate-spin" />}
            Save targets
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
