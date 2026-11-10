import { Compass, FolderOpen, Plus, Upload } from "lucide-react"
import { useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
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
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { Textarea } from "@/components/ui/textarea"
import { api, type CaseItem } from "@/lib/api"
import { cn } from "@/lib/utils"

export function CaseSidebar({
  cases,
  current,
  demo,
  onSelect,
  onCreated,
}: {
  cases: CaseItem[]
  current: string | null
  demo: boolean
  onSelect: (id: string) => void
  onCreated: (id: string) => void
}) {
  const [open, setOpen] = useState(false)

  async function openDemo() {
    try {
      const { id } = await api.openDemo()
      onCreated(id)
    } catch (e) {
      toast.error("Не удалось открыть демо", { description: (e as Error).message })
    }
  }

  return (
    <Sidebar data-testid="sidebar">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1.5 font-semibold">
          <Compass className="size-5" />
          Стратег
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Кейсы</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {cases.map((c) => (
                <SidebarMenuItem key={c.id}>
                  <SidebarMenuButton
                    isActive={c.id === current}
                    onClick={() => onSelect(c.id)}
                    data-testid="case-item"
                    title={c.title}
                  >
                    <span className="truncate">{c.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
              {cases.length === 0 && <p className="px-2 py-1 text-sm text-muted-foreground">Кейсов пока нет</p>}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="gap-2">
        {demo && (
          <Button variant="secondary" onClick={openDemo} data-testid="open-demo">
            <FolderOpen /> Открыть демо
          </Button>
        )}
        <Button onClick={() => setOpen(true)} data-testid="new-case">
          <Plus /> Новый кейс
        </Button>
      </SidebarFooter>
      <NewCaseDialog open={open} onOpenChange={setOpen} onCreated={onCreated} />
    </Sidebar>
  )
}

function NewCaseDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: (id: string) => void
}) {
  const [busy, setBusy] = useState(false)

  async function submit(form: HTMLFormElement) {
    setBusy(true)
    try {
      const { id } = await api.createCase(new FormData(form))
      toast.success(`Кейс ${id} создан`)
      onOpenChange(false)
      onCreated(id)
    } catch (e) {
      toast.error("Кейс не создан", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" data-testid="new-case-dialog">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void submit(e.currentTarget)
          }}
          className="grid gap-3"
        >
          <DialogHeader>
            <DialogTitle>Новый кейс</DialogTitle>
            <DialogDescription>Запрос — как есть, своими словами. Материалы — любые файлы: таблицы, отчёты, заметки.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="case_id">Короткое имя (латиница)</Label>
            <Input id="case_id" name="case_id" required pattern="[a-z0-9-]+" placeholder="retention-q4" data-testid="new-case-id" />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="title">Название</Label>
            <Input id="title" name="title" placeholder="Почему падает удержание" data-testid="new-case-title" />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="brief">Запрос</Label>
            <Textarea id="brief" name="brief" required rows={6} className="max-h-28" data-testid="new-case-brief" />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="files">Материалы</Label>
            <FilePicker id="files" testid="new-case-files" />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={busy} data-testid="new-case-create">
              Создать
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Выбор файлов: область «перетащите или нажмите» по-русски в любом браузере (системная кнопка подписана языком
 * браузера и показывает только число файлов) и список выбранных. */
export function FilePicker({ id, testid, required }: { id: string; testid: string; required?: boolean }) {
  const input = useRef<HTMLInputElement>(null)
  const [names, setNames] = useState<string[]>([])
  const [over, setOver] = useState(false)
  const pick = (list: FileList | null) => setNames([...(list ?? [])].map((f) => f.name))
  return (
    <div className="grid gap-2">
      <label
        htmlFor={id}
        data-testid={`${testid}-zone`}
        className={cn(
          "flex cursor-pointer items-center gap-3 rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground transition-colors hover:bg-muted/40",
          over && "border-primary bg-muted/60",
        )}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          if (input.current) input.current.files = e.dataTransfer.files
          pick(e.dataTransfer.files)
        }}
      >
        <Upload className="size-4 shrink-0" />
        {names.length ? `Выбрано файлов: ${names.length} — нажмите, чтобы заменить` : "Перетащите файлы сюда или нажмите, чтобы выбрать"}
      </label>
      <input
        ref={input}
        id={id}
        name="files"
        type="file"
        multiple
        required={required}
        className="sr-only"
        data-testid={testid}
        onChange={(e) => pick(e.target.files)}
      />
      <FileList names={names} />
    </div>
  )
}

function FileList({ names }: { names: string[] }) {
  if (!names.length) return null
  return (
    <div data-testid="file-list">
      <ul className="flex flex-wrap gap-1">
        {names.map((n) => (
          <li key={n} className="max-w-[9.5rem] truncate rounded-md border bg-muted/40 px-1.5 text-[11px] leading-5" title={n}>
            {n}
          </li>
        ))}
      </ul>
    </div>
  )
}
