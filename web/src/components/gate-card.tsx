import { ChevronDown } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Markdown } from "@/components/markdown"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { api, type Gate, type GateAnswer } from "@/lib/api"

const LEAD: Record<Gate["kind"], string> = {
  clarify: "Прежде чем искать, агент уточняет задачу. Ответьте или пропустите — он продолжит с допущениями.",
  research_plan: "Агент предлагает, что исследовать. Вопросы, которые не меняют выбор, он пропускает — это экономит время и деньги.",
  option_choice: "Варианты содержательно разные, среди них всегда «оставить как есть» и «закрыть». Выбираете вы.",
  decision_commit: "Решение фиксируется в журнале вместе с тем, кто его утвердил, и планом проверки.",
  side_effect: "Агент просит подтвердить действие.",
}

export function GateCard({ caseId, gate, demo, onAnswered }: { caseId: string; gate: Gate; demo: boolean; onAnswered: () => void }) {
  const [choice, setChoice] = useState(gate.default_choice ?? "")
  const [answers, setAnswers] = useState<string[]>(gate.questions.map(() => ""))
  const [approver, setApprover] = useState("руководитель продукта")
  const [busy, setBusy] = useState(false)

  async function send(answer: GateAnswer) {
    setBusy(true)
    try {
      await api.gate(caseId, answer)
      onAnswered()
    } catch (e) {
      toast.error("Ответ не принят", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const primary = {
    clarify: { label: answers.some(Boolean) ? "Ответить" : "Пропустить и продолжить", answer: { answers } },
    research_plan: { label: "Принять план", answer: { action: "approve" as const } },
    option_choice: { label: "Выбрать", answer: { choice } },
    decision_commit: { label: "Зафиксировать решение", answer: { action: "approve" as const, approver } },
    side_effect: { label: "Подтвердить", answer: { action: "approve" as const } },
  }[gate.kind]
  const planned = gate.plan?.filter((q) => q.planned) ?? []

  return (
    <Card className="mx-auto w-full max-w-3xl" data-testid="gate-card" data-gate={gate.kind}>
      <CardHeader>
        <Badge variant="outline" className="mb-1 w-fit">
          Нужно ваше решение
        </Badge>
        <CardTitle className="text-xl" data-testid="gate-title">
          {gate.title}
        </CardTitle>
        <CardDescription>{LEAD[gate.kind]}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5">
        {gate.kind === "clarify" &&
          gate.questions.map((q, i) => (
            <div key={i} className="grid gap-2" data-testid="gate-question">
              <Label htmlFor={`q${i}`} className="leading-snug font-normal">
                {q}
              </Label>
              <Input
                id={`q${i}`}
                value={answers[i]}
                disabled={demo}
                placeholder={demo ? "В демо ответы как при записи" : "Можно оставить пустым"}
                onChange={(e) => setAnswers(answers.map((a, j) => (j === i ? e.target.value : a)))}
              />
            </div>
          ))}

        {gate.kind === "research_plan" && gate.plan && (
          <div className="grid gap-3" data-testid="gate-plan">
            <p className="text-sm">
              Исследуем <b>{planned.length}</b> из {gate.plan.length} вопросов — остальные не меняют выбор.
            </p>
            <ul className="grid gap-2">
              {planned.map((q) => (
                <li key={q.id} className="rounded-lg border bg-muted/40 p-3 text-sm" data-testid="gate-plan-planned">
                  {q.question}
                  {q.why && <span className="mt-1 block text-xs text-muted-foreground">{q.why}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {gate.kind === "option_choice" && gate.choices && (
          <RadioGroup value={choice} onValueChange={setChoice} className="gap-2" data-testid="gate-options">
            {gate.choices.map((c) => (
              <Label
                key={c.id}
                htmlFor={c.id}
                className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 font-normal has-[[data-state=checked]]:border-primary has-[[data-state=checked]]:bg-primary/5"
                data-testid="gate-option"
              >
                <RadioGroupItem id={c.id} value={c.id} disabled={demo && c.id !== gate.default_choice} className="mt-0.5" />
                <span className="grid gap-1">
                  <span className="leading-snug">{c.title}</span>
                  <span className="flex flex-wrap gap-1.5">
                    <Badge variant="secondary">{c.kind}</Badge>
                    {c.id === gate.default_choice && <Badge data-testid="gate-default">предложение агента</Badge>}
                  </span>
                </span>
              </Label>
            ))}
          </RadioGroup>
        )}

        {gate.kind === "decision_commit" && (
          <div className="grid gap-3">
            <p className="text-sm" data-testid="gate-commit-option">
              Вариант: <b>{gate.option}</b>
            </p>
            <div className="grid gap-2">
              <Label htmlFor="approver">Кто утверждает</Label>
              <Input id="approver" value={approver} disabled={demo} onChange={(e) => setApprover(e.target.value)} data-testid="gate-approver" />
            </div>
          </div>
        )}

        {gate.details_md && (
          <Collapsible>
            <CollapsibleTrigger asChild>
              <Button variant="link" className="h-auto px-0 text-muted-foreground" data-testid="gate-details-toggle">
                Подробнее <ChevronDown />
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="pt-2" data-testid="gate-details">
              <Markdown className="prose-sm">{gate.details_md}</Markdown>
            </CollapsibleContent>
          </Collapsible>
        )}
      </CardContent>
      <CardFooter className="justify-end gap-2">
        {gate.kind === "decision_commit" && !demo && (
          <Button variant="ghost" disabled={busy} onClick={() => void send({ action: "reject", comment: "отклонено в панели" })}>
            Отклонить
          </Button>
        )}
        <Button disabled={busy || (gate.kind === "option_choice" && !choice)} onClick={() => void send(primary.answer)} data-testid="gate-primary">
          {primary.label}
        </Button>
      </CardFooter>
    </Card>
  )
}
