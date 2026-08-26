import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import App from "./App.tsx"
import { Toaster } from "@/components/ui/sonner"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    {/* theme="dark" is explicit (not left to next-themes' "system" default)
        because the whole app is dark-only - see index.html's class="dark" -
        so toasts should never flip light even if the OS is in light mode. */}
    <Toaster theme="dark" position="bottom-right" />
  </StrictMode>,
)
