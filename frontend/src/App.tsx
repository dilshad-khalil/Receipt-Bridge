/**
 * Top-level layout: a left sidebar nav (Printers / Logs / Settings) and
 * whichever page is active, tracked with plain `useState`.
 *
 * No router on purpose - this is a small local config tool for one machine
 * with three flat views, not a real multi-route site, so react-router would
 * be pure overhead (extra dependency, history/URL state to manage) for
 * something a single `useState` already does perfectly well.
 */
import { useState } from "react"
import { Printer, ScrollText, Settings as SettingsIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import { useHealth } from "@/hooks/usePrintBridge"
import { PrintersPage } from "@/pages/PrintersPage"
import { LogsPage } from "@/pages/LogsPage"
import { SettingsPage } from "@/pages/SettingsPage"

type View = "printers" | "logs" | "settings"

const NAV: Array<{ id: View; label: string; icon: typeof Printer }> = [
  { id: "printers", label: "Printers", icon: Printer },
  { id: "logs", label: "Logs", icon: ScrollText },
  { id: "settings", label: "Settings", icon: SettingsIcon },
]

/** Small connection indicator in the sidebar header, backed by `GET /health`. */
function StatusPill() {
  const { data, loading, error } = useHealth()

  if (loading) {
    return <span className="text-xs text-muted-foreground">Checking server...</span>
  }
  if (error || !data) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-red-400">
        <span className="size-1.5 shrink-0 rounded-full bg-red-500" />
        Not reachable
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="size-1.5 shrink-0 rounded-full bg-emerald-500" />
      Running &middot; v{data.version}
    </span>
  )
}

export default function App() {
  const [view, setView] = useState<View>("printers")

  return (
    <div className="flex min-h-svh bg-background text-foreground">
      <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-card/40 p-3">
        <div className="mb-6 px-2 pt-2">
          <div className="font-heading text-sm font-semibold tracking-tight">Print Bridge</div>
          <div className="mt-1">
            <StatusPill />
          </div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setView(id)}
              className={cn(
                "flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm transition-colors",
                view === id
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
              )}
            >
              <Icon className="size-4" />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-auto px-2 pb-1 text-[11px] text-muted-foreground/70">
          Local only &middot; 127.0.0.1
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-4xl">
          {view === "printers" && <PrintersPage />}
          {view === "logs" && <LogsPage />}
          {view === "settings" && <SettingsPage />}
        </div>
      </main>
    </div>
  )
}
