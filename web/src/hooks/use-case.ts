import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { api, subscribe, type CaseView, type LiveEvent } from "@/lib/api"

const STEP_LABELS: Record<string, string> = {
  intake: "Разбирает запрос",
  clarify: "Готовит вопросы",
  research_plan: "Составляет план исследования",
  research_gate: "Ждёт решения по плану",
  read: "Читает материалы",
  merge: "Сводит факты",
  research_loop: "Ищет недостающее",
  claims: "Проверяет утверждения",
  situation: "Описывает ситуацию",
  options: "Собирает варианты",
  stress: "Проводит стресс-тест",
  option_gate: "Ждёт выбора варианта",
  product: "Пишет продуктовую стратегию",
  comms: "Пишет коммуникационную стратегию",
  promise_lint: "Сверяет обещания с продуктом",
  experiments: "Составляет план проверки",
  forecast: "Делает прогнозы",
  commit: "Фиксирует решение",
  summary: "Пишет выжимку для руководителя",
  report: "Собирает отчёт",
  revise: "Пересматривает затронутые выводы",
}

/** «read:SRC-003» → «Читает материалы», «comms:1» → «Пишет коммуникационную стратегию». */
export function stepLabel(step: string): string {
  return STEP_LABELS[step.split(":")[0]] ?? step
}

export function useCase(caseId: string | null) {
  const [data, setData] = useState<CaseView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState<LiveEvent[]>([])
  const timer = useRef<number | null>(null)

  const reload = useCallback(async () => {
    if (!caseId) return
    try {
      setData(await api.case(caseId))
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [caseId])

  useEffect(() => {
    setData(null)
    setLive([])
    void reload()
  }, [reload])

  // Живые события: шаги агента идут в «ход работы», конец прогона — перечитать кейс и сказать, чем кончилось
  useEffect(() => {
    if (!caseId) return
    return subscribe((e) => {
      if (e.case_id !== caseId) return
      if (e.type.startsWith("step.") || e.type === "injection.detected") setLive((prev) => [...prev.slice(-40), e])
      if (e.type === "job.finished") {
        const error = String(e.data.error ?? "")
        if (error) toast.error("Агент остановился с ошибкой", { description: error })
        void reload()
      }
      if (e.type === "gate.waiting" || e.type === "revision.completed") void reload()
    })
  }, [caseId, reload])

  // Запасной путь без SSE: пока идёт прогон, кейс перечитывается раз в секунду
  useEffect(() => {
    if (timer.current) window.clearInterval(timer.current)
    if (data?.job?.running) timer.current = window.setInterval(() => void reload(), 1000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [data?.job?.running, reload])

  return { data, error, live, reload }
}
