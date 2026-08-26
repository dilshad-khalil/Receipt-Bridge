/**
 * Settings page: port, allowed CORS origins, auth token, and the "start
 * with Windows" toggle (mirrors the tray menu's equivalent checkbox - see
 * app/startup.py, shared by both).
 *
 * Port and allowed-origins changes only take effect after a restart (the
 * HTTP server and CORS middleware are built once at startup - see
 * app/main.py's create_app()/BridgeServer) - this page makes that
 * explicit rather than implying the change is live immediately.
 */
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Loader2, Plus, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { api, ApiError, getToken, setToken as persistToken } from "@/lib/api"
import { useConfig } from "@/hooks/usePrintBridge"

function errMsg(err: unknown): string {
  return err instanceof ApiError || err instanceof Error ? err.message : String(err)
}

export function SettingsPage() {
  const { data: config, loading, error, refresh } = useConfig()

  const [initialized, setInitialized] = useState(false)
  const [port, setPort] = useState("9187")
  const [origins, setOrigins] = useState<string[]>([])
  const [tokenField, setTokenField] = useState(getToken())
  const [rateLimit, setRateLimit] = useState("60")
  const [saving, setSaving] = useState(false)
  const [startupBusy, setStartupBusy] = useState(false)

  // Seed the editable fields from the server exactly once, when config
  // first loads - not on every refresh, so an in-progress edit on this page
  // (e.g. after toggling "Start with Windows", which calls refresh()) isn't
  // silently overwritten.
  useEffect(() => {
    if (config && !initialized) {
      setPort(String(config.port))
      setOrigins(config.allowed_origins.length > 0 ? config.allowed_origins : [""])
      setRateLimit(String(config.rate_limit_per_minute))
      setInitialized(true)
    }
  }, [config, initialized])

  const updateOrigin = (index: number, value: string) => {
    setOrigins((prev) => prev.map((o, i) => (i === index ? value : o)))
  }
  const removeOrigin = (index: number) => {
    setOrigins((prev) => prev.filter((_, i) => i !== index))
  }
  const addOrigin = () => setOrigins((prev) => [...prev, ""])

  const handleUseToken = () => {
    persistToken(tokenField.trim())
    toast.success("Token saved in this browser for future requests to this page.")
  }

  const handleSaveSettings = async () => {
    const portNum = Number(port)
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) {
      toast.error("Port must be a number between 1 and 65535")
      return
    }
    const rateLimitNum = Number(rateLimit)
    if (!Number.isInteger(rateLimitNum) || rateLimitNum < 1) {
      toast.error("Rate limit must be a positive whole number")
      return
    }
    setSaving(true)
    try {
      await api.updateSettings({
        port: portNum,
        allowed_origins: origins.map((o) => o.trim()).filter(Boolean),
        auth_token: tokenField.trim() || null,
        rate_limit_per_minute: rateLimitNum,
      })
      toast.success(
        "Settings saved. Restart Print Bridge from the tray icon for the port/origins change to take effect.",
      )
      refresh()
    } catch (err) {
      toast.error(`Could not save settings: ${errMsg(err)}`)
    } finally {
      setSaving(false)
    }
  }

  const handleToggleStartup = async (checked: boolean) => {
    setStartupBusy(true)
    try {
      await api.setStartupEnabled(checked)
      toast.success(checked ? "Print Bridge will start with Windows" : "Startup shortcut removed")
      refresh()
    } catch (err) {
      toast.error(`Could not update startup setting: ${errMsg(err)}`)
    } finally {
      setStartupBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-heading text-lg font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">Server, CORS, and startup configuration.</p>
      </div>

      {loading && !config && <p className="text-sm text-muted-foreground">Loading...</p>}
      {error && <p className="text-sm text-red-400">Could not load settings: {error}</p>}

      {config && (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Server</CardTitle>
              <CardDescription>
                Port and allowed origins require restarting Print Bridge (tray icon &rarr;
                &quot;Restart server&quot;) to take effect - both are only read once at startup.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="max-w-40">
                <Label htmlFor="port" className="mb-1.5">
                  Port
                </Label>
                <Input
                  id="port"
                  type="number"
                  min={1}
                  max={65535}
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                />
              </div>

              <div>
                <Label className="mb-1.5">Allowed origins</Label>
                <p className="mb-2 text-xs text-muted-foreground">
                  Exact match, e.g. <code>https://ourapp.example.com</code>. A page opened via{" "}
                  <code>file://</code> is always allowed too, regardless of this list.
                </p>
                <div className="flex flex-col gap-2">
                  {origins.map((origin, i) => (
                    <div key={i} className="flex gap-2">
                      <Input
                        placeholder="https://ourapp.example.com"
                        value={origin}
                        onChange={(e) => updateOrigin(i, e.target.value)}
                      />
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={() => removeOrigin(i)}
                        aria-label="Remove origin"
                      >
                        <X />
                      </Button>
                    </div>
                  ))}
                </div>
                <Button variant="outline" size="sm" className="mt-2" onClick={addOrigin}>
                  <Plus />
                  Add origin
                </Button>
              </div>

              <div className="max-w-40">
                <Label htmlFor="rate-limit" className="mb-1.5">
                  Rate limit (req/min)
                </Label>
                <p className="mb-2 text-xs text-muted-foreground">
                  Caps requests to <code>/print/*</code> per minute, keyed by the auth token (if
                  set) or by the caller&apos;s origin. Takes effect immediately - no restart
                  needed.
                </p>
                <Input
                  id="rate-limit"
                  type="number"
                  min={1}
                  value={rateLimit}
                  onChange={(e) => setRateLimit(e.target.value)}
                />
              </div>

              <div>
                <Label htmlFor="token" className="mb-1.5">
                  Auth token
                </Label>
                <p className="mb-2 text-xs text-muted-foreground">
                  Optional - leave blank to disable auth. Also used by this page itself for its
                  own API calls once saved to this browser.
                </p>
                <div className="flex gap-2">
                  <Input
                    id="token"
                    placeholder="shared secret for X-Print-Bridge-Token header"
                    value={tokenField}
                    onChange={(e) => setTokenField(e.target.value)}
                  />
                  <Button variant="outline" onClick={handleUseToken}>
                    Use this token
                  </Button>
                </div>
              </div>

              <div>
                <Button onClick={handleSaveSettings} disabled={saving}>
                  {saving && <Loader2 className="animate-spin" />}
                  Save settings
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Startup</CardTitle>
              <CardDescription>Takes effect immediately - no restart needed.</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm">Start with Windows</p>
                  <p className="text-xs text-muted-foreground">
                    Adds/removes a shortcut in your Startup folder.
                  </p>
                </div>
                <Switch
                  checked={config.startup_enabled}
                  onCheckedChange={handleToggleStartup}
                  disabled={startupBusy}
                />
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
