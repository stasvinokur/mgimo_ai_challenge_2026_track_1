import { Search } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { toast } from "sonner"

import { ClaimText } from "@/components/claim-text"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api, plural, type Fact, type Preview } from "@/lib/api"

/** «опора довода выжимки · держит узлов: 44 — …» → короткий бейдж «опора выжимки». */
function why(f: Fact): string {
  const w = f.why_first.toLowerCase()
  if (w.includes("выжимк")) return "опора выжимки"
  if (w.includes("уязвим")) return "уязвимое основание"
  if (w.includes("неувер")) return "низкая уверенность"
  return "держит выводы"
}

export function FactsTab({
  caseId,
  version,
  open,
  onOpenChange,
  onContested,
}: {
  caseId: string
  version: number
  open: string | null
  onOpenChange: (claimId: string | null) => void
  onContested: () => void
}) {
  const [facts, setFacts] = useState<Fact[] | null>(null)
  const [query, setQuery] = useState("")

  useEffect(() => {
    setFacts(null)
    api.facts(caseId).then(setFacts, (e: Error) => toast.error("Факты не загрузились", { description: e.message }))
  }, [caseId, version])

  const shown = useMemo(
    () => (facts ?? []).filter((f) => !query || `${f.claim_id} ${f.text}`.toLowerCase().includes(query.toLowerCase())),
    [facts, query],
  )
  const current = facts?.find((f) => f.claim_id === open) ?? null

  return (
    <div className="mx-auto grid w-full max-w-3xl gap-4" data-testid="facts">
      <p className="text-sm text-muted-foreground">
        Утверждения, на которых держится решение. У каждого — источник, «за» и «против». Можно проверить, что будет,
        если факт неверен.
      </p>
      <div className="relative">
        <Search className="absolute top-2.5 left-3 size-4 text-muted-foreground" />
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Поиск по фактам" className="pl-9" data-testid="facts-search" />
      </div>
      {facts === null && <Skeleton className="h-40" />}
      {facts?.length === 0 && <p className="text-sm text-muted-foreground">Несущих утверждений пока нет: они появятся вместе с вариантами.</p>}
      <ul className="grid gap-2">
        {shown.map((f) => (
          <li key={f.claim_id}>
            <button
              type="button"
              onClick={() => onOpenChange(f.claim_id)}
              className="grid w-full gap-1.5 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted/50"
              data-testid="fact-row"
              data-claim={f.claim_id}
            >
              <span className="flex items-center gap-2">
                <Badge variant="outline" className="font-mono">{f.claim_id}</Badge>
                <Badge variant="secondary">{why(f)}</Badge>
                <span className="ml-auto text-xs text-muted-foreground">держит выводов: {f.dependents}</span>
              </span>
              <span className="line-clamp-2">
                <ClaimText text={f.text.replace(/\s*\[расчёт:[^\]]*\]/, "")} />
              </span>
            </button>
          </li>
        ))}
      </ul>
      <FactSheet caseId={caseId} fact={current} onClose={() => onOpenChange(null)} onContested={onContested} />
    </div>
  )
}

function FactSheet({ caseId, fact, onClose, onContested }: { caseId: string; fact: Fact | null; onClose: () => void; onContested: () => void }) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [reason, setReason] = useState("")
  useEffect(() => {
    setPreview(null)
    setReason("")
  }, [fact?.claim_id])

  return (
    <Sheet open={fact !== null} onOpenChange={(v) => !v && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-xl" data-testid="fact-sheet">
        {fact && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2">
                <Badge variant="outline" className="font-mono">{fact.claim_id}</Badge>
                {fact.type_label}
              </SheetTitle>
              <SheetDescription>
                Опора: {fact.support} · надёжность: {fact.reliability}
              </SheetDescription>
            </SheetHeader>
            <ScrollArea className="h-[calc(100vh-7rem)] px-4">
              <div className="grid gap-5 pb-8 text-sm">
                <p className="leading-relaxed" data-testid="fact-text">{fact.text.replace(/\s*\[расчёт:[^\]]*\]/, "")}</p>
                <section className="grid gap-2" data-testid="fact-sources">
                  <p className="text-xs text-muted-foreground uppercase">Источник</p>
                  {fact.sources.map((s, i) => (
                    <div key={i} className="rounded-lg border p-3">
                      <p className="font-medium">
                        {s.source_id} · {s.title}
                        {s.synthetic && <Badge variant="outline" className="ml-2">синтетика</Badge>}
                      </p>
                      {s.is_calculation ? (
                        <>
                          <p className="mt-1 text-muted-foreground">
                            Расчёт кодом{s.period && `, период ${s.period}`}{s.rows !== null && `, строк: ${s.rows}`}
                          </p>
                          {fact.text.match(/\[расчёт:([^\]]*)\]/) && (
                            <p className="mt-1 font-mono text-xs text-muted-foreground" data-testid="fact-how">
                              как посчитано: {fact.text.match(/\[расчёт:([^\]]*)\]/)?.[1].trim()}
                            </p>
                          )}
                        </>
                      ) : (
                        s.quote && <blockquote className="mt-1 border-l-2 pl-3 text-muted-foreground">«{s.quote}»</blockquote>
                      )}
                    </div>
                  ))}
                </section>
                <section className="grid gap-3">
                  <Button
                    variant="outline"
                    data-testid="fact-challenge"
                    onClick={async () => setPreview(await api.challenge(caseId, [fact.claim_id]))}
                  >
                    Что если этот факт неверен?
                  </Button>
                  {preview && (
                    <Alert data-testid="fact-impact">
                      <AlertTitle>
                        Потеряют опору: {plural(preview.lost.length, "вывод", "вывода", "выводов")}
                      </AlertTitle>
                      <AlertDescription className="grid gap-1">
                        {[...preview.lost]
                          .sort((a, b) => Number(b.kind === "решение") - Number(a.kind === "решение"))
                          .filter((l) => l.kind === "решение" || l.direct)
                          .slice(0, 6)
                          .map((l) => (
                            <span key={l.node_id} data-testid={l.kind === "решение" ? "fact-impact-decision" : undefined}>
                              {l.kind} {l.node_id} — {l.effect}
                            </span>
                          ))}
                        <span className="text-xs">Проверка без моделей, кейс не меняется.</span>
                      </AlertDescription>
                    </Alert>
                  )}
                  {preview && (
                    <div className="grid gap-2">
                      <Textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Почему факт неверен" data-testid="fact-reason" />
                      <Button
                        disabled={!reason.trim()}
                        data-testid="fact-contest"
                        onClick={async () => {
                          try {
                            await api.contest(caseId, [fact.claim_id], reason)
                            onClose()
                            onContested()
                          } catch (e) {
                            toast.error("Пересмотр не запущен", { description: (e as Error).message })
                          }
                        }}
                      >
                        Оспорить и пересмотреть
                      </Button>
                    </div>
                  )}
                </section>
                <Separator />
                {(fact.pro.length > 0 || fact.con.length > 0) && <ProCon pro={fact.pro} con={fact.con} />}
              </div>
            </ScrollArea>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function ProCon({ pro, con }: { pro: string[]; con: string[] }) {
  const [all, setAll] = useState(false)
  const items = [...pro.map((t) => ({ ok: true, t })), ...con.map((t) => ({ ok: false, t }))]
  // свёрнуто — по одному доводу «за» и «против», если есть оба: «за и против» не должно выглядеть как только «за»
  const first = [items.find((x) => x.ok), items.find((x) => !x.ok)].filter((x) => x !== undefined)
  const shown = all ? items : first.length === 2 ? first : items.slice(0, 2)
  return (
    <section className="grid gap-2" data-testid="fact-procon">
      <p className="text-xs text-muted-foreground uppercase">За и против</p>
      {shown.map((x, i) => (
        <p key={i} className={x.ok ? "" : "text-muted-foreground"}>
          {x.ok ? "✅" : "⚠️"} {x.t}
        </p>
      ))}
      {items.length > 2 && (
        <Button variant="link" className="h-auto w-fit px-0 text-muted-foreground" onClick={() => setAll(!all)}>
          {all ? "Свернуть" : `Ещё ${items.length - 2}`}
        </Button>
      )}
    </section>
  )
}
