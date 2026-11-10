import { ChevronDown, Loader2, Play, RefreshCcw } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { ClaimText } from "@/components/claim-text"
import { GateCard } from "@/components/gate-card"
import { stepLabel } from "@/hooks/use-case"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, type CaseView, type LiveEvent, type Revision } from "@/lib/api"

export function DecisionTab({
  data,
  live,
  demo,
  onChanged,
  onOpenFact,
}: {
  data: CaseView
  live: LiveEvent[]
  demo: boolean
  onChanged: () => void
  onOpenFact: (claimId: string) => void
}) {
  const running = data.job?.running ?? false
  if (running) return <Working data={data} live={live} />
  if (data.gate) return <GateCard key={`${data.gate.kind}-${data.version}`} caseId={data.id} gate={data.gate} demo={demo} onAnswered={onChanged} />
  if (!data.finished) return <Start data={data} onStarted={onChanged} />
  return (
    <div className="mx-auto grid w-full max-w-3xl gap-6">
      {data.revision && <RevisionBanner revision={data.revision} onOpenFact={onOpenFact} />}
      <Answer data={data} onOpenFact={onOpenFact} />
    </div>
  )
}

function Start({ data, onStarted }: { data: CaseView; onStarted: () => void }) {
  const [busy, setBusy] = useState(false)
  return (
    <Card className="mx-auto w-full max-w-3xl" data-testid="start-card">
      <CardHeader>
        <CardTitle className="text-xl">Кейс готов к работе</CardTitle>
        <CardDescription>
          Агент разберёт запрос и материалы, задаст вопросы, предложит план и варианты. На важных шагах он остановится и
          спросит вас.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex justify-end">
        <Button
          size="lg"
          disabled={busy}
          data-testid="run"
          onClick={async () => {
            setBusy(true)
            try {
              await api.run(data.id)
              onStarted()
            } catch (e) {
              toast.error("Не запустилось", { description: (e as Error).message })
            } finally {
              setBusy(false)
            }
          }}
        >
          <Play /> Запустить
        </Button>
      </CardContent>
    </Card>
  )
}

function Working({ data, live }: { data: CaseView; live: LiveEvent[] }) {
  const done = live.filter((e) => e.type === "step.completed").length
  const last = [...live].reverse().find((e) => e.type === "step.started" || e.type === "step.completed")
  const injected = live.find((e) => e.type === "injection.detected")
  const current = last ? stepLabel(String(last.data.step ?? "")) : data.job?.kind === "revise" ? "Пересматривает затронутые выводы" : "Начинает"
  return (
    <Card className="mx-auto w-full max-w-3xl" data-testid="working-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-xl">
          <Loader2 className="size-5 animate-spin" /> Агент работает
        </CardTitle>
        <CardDescription data-testid="working-step">{current}…</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <Progress value={Math.min(95, 5 + done * 3)} />
        {injected && (
          <Alert data-testid="injection-alert">
            <AlertTitle>В материалах найдена команда для ИИ</AlertTitle>
            <AlertDescription>Агент её не выполняет, а записывает как попытку вмешательства.</AlertDescription>
          </Alert>
        )}
        <ol className="grid gap-1 text-sm text-muted-foreground">
          {live
            .filter((e) => e.type === "step.completed")
            .slice(-5)
            .map((e, i) => (
              <li key={i}>✓ {stepLabel(String(e.data.step ?? ""))}</li>
            ))}
        </ol>
      </CardContent>
    </Card>
  )
}

function Answer({ data, onOpenFact }: { data: CaseView; onOpenFact: (id: string) => void }) {
  const s = data.executive
  if (!s) return <p className="text-muted-foreground">Выжимки ещё нет — она появится в конце работы агента.</p>
  return (
    <article className="grid gap-8" data-testid="answer">
      <section className="grid gap-2">
        <p className="text-sm text-muted-foreground">Ответ</p>
        <p className="text-xl leading-relaxed font-medium" data-testid="answer-text">
          <ClaimText text={s.answer} onOpen={onOpenFact} />
        </p>
      </section>
      <section className="grid gap-3">
        <p className="text-sm text-muted-foreground">Почему</p>
        <ol className="grid gap-3" data-testid="answer-reasons">
          {s.reasons.map((r, i) => (
            <li key={i} className="grid gap-1 rounded-lg border p-4">
              <ClaimText text={r.text} claimIds={r.claim_ids} onOpen={onOpenFact} />
              {(r.kind || r.caveat) && (
                <span className="text-xs text-muted-foreground">
                  {r.kind && <>Опора: {r.kind}{r.source && ` · ${r.source}`}</>}
                  {r.caveat && <> · оговорка: {r.caveat}</>}
                </span>
              )}
            </li>
          ))}
        </ol>
      </section>
      {s.next_steps.length > 0 && (
        <section className="grid gap-3">
          <p className="text-sm text-muted-foreground">Что делаем дальше</p>
          <Table data-testid="answer-steps">
            <TableHeader>
              <TableRow>
                <TableHead className="w-28">Когда</TableHead>
                <TableHead>Что</TableHead>
                <TableHead className="w-40">Кто</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.next_steps.map((st, i) => (
                <TableRow key={i}>
                  <TableCell className="align-top whitespace-nowrap">{st.when}</TableCell>
                  <TableCell className="whitespace-normal">{st.action}</TableCell>
                  <TableCell className="align-top whitespace-normal text-muted-foreground">{st.owner}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}
      {s.say_to_team && (
        <section className="grid gap-2" data-testid="answer-team">
          <p className="text-sm text-muted-foreground">Что сказать команде</p>
          <blockquote className="border-l-2 pl-4 italic">«{s.say_to_team.replace(/^«|»$/g, "")}»</blockquote>
        </section>
      )}
    </article>
  )
}

function RevisionBanner({ revision, onOpenFact }: { revision: Revision; onOpenFact: (id: string) => void }) {
  return (
    <Collapsible>
      <Alert data-testid="revision-banner">
        <RefreshCcw />
        <AlertTitle>{revision.reopened ? "Решение открыто заново — что изменилось" : "Выводы пересмотрены"}</AlertTitle>
        <AlertDescription className="grid gap-2">
          <span>
            Пересмотрено выводов: {revision.revised.length}, не тронуто: {revision.untouched}
            {revision.token_share !== null && ` · ${Math.round(revision.token_share * 100)}% токенов полного прогона`}
          </span>
          <CollapsibleTrigger asChild>
            <Button variant="link" className="h-auto w-fit px-0" data-testid="revision-toggle">
              Почему изменились <ChevronDown />
            </Button>
          </CollapsibleTrigger>
        </AlertDescription>
      </Alert>
      <CollapsibleContent className="grid gap-3 pt-3" data-testid="revision-details">
        {revision.revised.map((r) => (
          <Card key={r.id} className="gap-2 py-4" data-testid="revision-card">
            <CardContent className="grid gap-2 text-sm">
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="font-mono">{r.id}</Badge>
                <span className="text-muted-foreground">{r.kind}</span>
              </div>
              <ClaimText text={r.reason} onOpen={onOpenFact} />
              <Collapsible>
                <CollapsibleTrigger asChild>
                  <Button variant="link" className="h-auto w-fit px-0 text-xs text-muted-foreground">
                    Было и стало <ChevronDown />
                  </Button>
                </CollapsibleTrigger>
                <CollapsibleContent className="grid gap-2 pt-1 text-xs md:grid-cols-2">
                  <p className="rounded bg-muted/50 p-2">{r.before}</p>
                  <p className="rounded bg-primary/5 p-2">{r.after}</p>
                </CollapsibleContent>
              </Collapsible>
            </CardContent>
          </Card>
        ))}
      </CollapsibleContent>
    </Collapsible>
  )
}
