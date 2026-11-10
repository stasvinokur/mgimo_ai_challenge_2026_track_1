import { Bell, FilePlus2, ListChecks } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { stepLabel } from "@/hooks/use-case"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { FilePicker } from "@/components/case-sidebar"
import { api, pct, plural, rub, type CaseView } from "@/lib/api"

export function CaseHeader({ data, demo, onChanged }: { data: CaseView; demo: boolean; onChanged: () => void }) {
  const [journal, setJournal] = useState(false)
  const [adding, setAdding] = useState(false)
  const m = data.metrics
  const busy = data.job?.running ?? false

  return (
    <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-6 py-3" data-testid="case-header">
      <SidebarTrigger className="-ml-2" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h1 className="truncate text-base font-semibold" title={data.title} data-testid="case-title">
            {data.title}
          </h1>
          <Badge variant="secondary" data-testid="case-stage">
            {data.finished ? "Готово" : data.stage_label}
          </Badge>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <p className="w-fit text-sm text-muted-foreground" data-testid="case-metrics">
              {plural(m.claims, "факт", "факта", "фактов")} · {pct(m.unsupported_share)} без источника · {demo ? "демо без моделей" : rub(m.rub)}
            </p>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="max-w-xs">
            Утверждений в таблице фактов: {m.claims}. Без источника: {pct(m.unsupported_share)}. Точность цитат:{" "}
            {pct(m.citation_accuracy)}. Источников: {m.sources}. Вызовов моделей: {m.calls}, {rub(m.rub)}.
          </TooltipContent>
        </Tooltip>
      </div>
      <div className="flex items-center gap-1">
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="icon" className="relative" data-testid="agenda" aria-label="Что решать сейчас">
              <Bell />
              {data.agenda.length > 0 && (
                <span className="absolute top-1 right-1 size-2 rounded-full bg-primary" data-testid="agenda-dot" />
              )}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-80" data-testid="agenda-list">
            <p className="mb-2 text-sm font-medium">Что решать сейчас</p>
            {data.agenda.length === 0 && <p className="text-sm text-muted-foreground">Сейчас решать нечего.</p>}
            <ul className="grid gap-2 text-sm">
              {data.agenda.map((a, i) => (
                <li key={i}>
                  {a.title}
                  {a.due && <span className="text-muted-foreground"> · до {a.due}</span>}
                </li>
              ))}
            </ul>
          </PopoverContent>
        </Popover>
        <Button variant="ghost" size="icon" onClick={() => setJournal(true)} aria-label="Журнал шагов" data-testid="journal">
          <ListChecks />
        </Button>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => setAdding(true)} data-testid="add-data">
          <FilePlus2 /> Добавить данные
        </Button>
      </div>

      <Sheet open={journal} onOpenChange={setJournal}>
        <SheetContent data-testid="journal-sheet">
          <SheetHeader>
            <SheetTitle>Журнал</SheetTitle>
            <SheetDescription>Каждый шаг агента записан: работу можно прервать и продолжить.</SheetDescription>
          </SheetHeader>
          <ScrollArea className="h-[calc(100vh-8rem)] px-4">
            <ol className="grid gap-1.5 text-sm">
              {data.steps.map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-muted-foreground">✓</span>
                  <span>{stepLabel(s)}</span>
                  <span className="ml-auto font-mono text-xs text-muted-foreground">{s}</span>
                </li>
              ))}
            </ol>
          </ScrollArea>
        </SheetContent>
      </Sheet>
      <AddDataDialog open={adding} onOpenChange={setAdding} caseId={data.id} demo={demo} onStarted={onChanged} />
    </header>
  )
}

function AddDataDialog({
  open,
  onOpenChange,
  caseId,
  demo,
  onStarted,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  caseId: string
  demo: boolean
  onStarted: () => void
}) {
  const [busy, setBusy] = useState(false)
  async function submit(form: HTMLFormElement) {
    setBusy(true)
    try {
      await api.evidence(caseId, new FormData(form))
      onOpenChange(false)
      onStarted()
    } catch (e) {
      toast.error("Пересмотр не запущен", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto" data-testid="add-data-dialog">
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            void submit(e.currentTarget)
          }}
        >
          <DialogHeader>
            <DialogTitle>Новые данные</DialogTitle>
            <DialogDescription>
              Агент пересмотрит только выводы, которые от них зависят, и покажет, что изменилось.
              {demo && " В демо подаются итоги A/B-теста из записи."}
            </DialogDescription>
          </DialogHeader>
          <FilePicker id="evidence-files" testid="add-data-files" required={!demo} />
          <DialogFooter>
            <Button type="submit" disabled={busy} data-testid="add-data-submit">
              Пересмотреть затронутые выводы
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
