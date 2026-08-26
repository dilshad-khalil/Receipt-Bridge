/**
 * Preview dialog: shows the page image(s) from a `dry_run: true` response
 * (POST /print/text or POST /print/pdf) - built on shadcn's Dialog, the
 * same "modal over the existing page" pattern already used by
 * ConfirmDialog, plus ScrollArea (already used on the Logs page) so a
 * multi-page PDF preview scrolls within the dialog instead of growing past
 * the viewport.
 */
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"

interface PreviewDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  /** Base64 PNGs, one per page - as returned in
   * `preview_images_base64` (no printer was touched to produce these). */
  images: string[]
}

export function PreviewDialog({ open, onOpenChange, title, description, images }: PreviewDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[65vh]">
          <div className="flex flex-col items-center gap-3 py-1">
            {images.map((base64, i) => (
              <img
                key={i}
                src={`data:image/png;base64,${base64}`}
                alt={images.length > 1 ? `Page ${i + 1} preview` : "Preview"}
                className="w-full border border-border"
              />
            ))}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}
