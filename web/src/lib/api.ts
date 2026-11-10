// Клиент API панели (src/strategist/interfaces/web/api.py). Типы повторяют то, что отдаёт views.py.

export type CaseItem = { id: string; title: string; stage: string; updated_at: string }

export type Choice = { id: string; title: string; kind: string }
export type PlanQuestion = { id: string; question: string; planned: boolean; why: string }

export type Gate = {
  kind: "clarify" | "research_plan" | "option_choice" | "decision_commit" | "side_effect"
  title: string
  details_md: string
  questions: string[]
  default_choice: string | null
  choices?: Choice[]
  plan?: PlanQuestion[]
  option?: string
}

export type Reason = { text: string; kind: string; source: string; caveat: string; claim_ids: string[] }
export type Executive = {
  answer: string
  reasons: Reason[]
  next_steps: { when: string; action: string; owner: string; metric: string }[]
  asks: string[]
  revisit: string
  say_to_team: string
}

export type Revision = {
  summary: string
  reopened: boolean
  contested: string[]
  revised: { id: string; kind: string; before: string; after: string; reason: string }[]
  untouched: number
  signals: { statement?: string; condition?: string }[]
  tokens: number
  token_share: number | null
}

export type Job = {
  case_id: string
  kind: string
  running: boolean
  started_at: string
  finished_at: string | null
  error: string
}

export type CaseView = {
  id: string
  title: string
  brief: string
  stage: string
  stage_label: string
  version: number
  finished: boolean
  gate: Gate | null
  steps: string[]
  metrics: {
    claims: number
    unsupported_share: number
    citation_accuracy: number | null
    sources: number
    rub: number
    usd: number
    calls: number
  }
  executive: Executive | null
  agenda: { title: string; detail: string; due: string | null }[]
  revision: Revision | null
  job: Job | null
}

export type SourceRef = {
  source_id: string
  title: string
  synthetic: boolean
  quote: string
  locator: string
  cells: string[]
  period: string | null
  rows: number | null
  is_calculation: boolean
}

export type Fact = {
  claim_id: string
  text: string
  type_label: string
  support: string
  reliability: string
  why_first: string
  sources: SourceRef[]
  pro: string[]
  con: string[]
  dependents: number
}

export type Impact = { node_id: string; kind: string; label: string; effect: string; direct: boolean }
export type Preview = { removed: string[]; unknown: string[]; lost: Impact[]; holding: Impact[]; summary: string }

export type GateAnswer = {
  action?: "approve" | "edit" | "reject"
  choice?: string | null
  answers?: string[]
  comment?: string
  approver?: string
  edits?: Record<string, string[]>
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(typeof body.detail === "string" ? body.detail : res.statusText)
  }
  const type = res.headers.get("content-type") ?? ""
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
})

export const api = {
  meta: () => call<{ demo: boolean; demo_case_id: string }>("/api/meta"),
  cases: () => call<CaseItem[]>("/api/cases"),
  createCase: (form: FormData) => call<{ id: string }>("/api/cases", { method: "POST", body: form }),
  openDemo: () => call<{ id: string }>("/api/cases/demo", { method: "POST" }),
  case: (id: string) => call<CaseView>(`/api/cases/${id}`),
  run: (id: string) => call<Job>(`/api/cases/${id}/run`, { method: "POST" }),
  gate: (id: string, answer: GateAnswer) => call<Job>(`/api/cases/${id}/gate`, json(answer)),
  evidence: (id: string, form: FormData) => call<Job>(`/api/cases/${id}/evidence`, { method: "POST", body: form }),
  facts: (id: string) => call<Fact[]>(`/api/cases/${id}/facts`),
  challenge: (id: string, claimIds: string[]) => call<Preview>(`/api/cases/${id}/challenge`, json({ claim_ids: claimIds })),
  contest: (id: string, claimIds: string[], reason: string) =>
    call<Job>(`/api/cases/${id}/contest`, json({ claim_ids: claimIds, reason })),
  report: (id: string) => call<string>(`/api/cases/${id}/report`),
}

export type LiveEvent = { type: string; case_id: string; at?: string; data: Record<string, unknown> }

/** Живые события агента (SSE). Возвращает функцию отписки. */
export function subscribe(onEvent: (e: LiveEvent) => void): () => void {
  const source = new EventSource("/api/events")
  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as LiveEvent)
    } catch {
      /* служебное сообщение */
    }
  }
  return () => source.close()
}

/** Строка реестра в тексте довода: «… [CL-007]» → текст и коды отдельно, коды показываются чипами. */
export function splitClaims(text: string): { text: string; ids: string[] } {
  const ids = [...text.matchAll(/\[(CL-\d+)\]/g)].map((m) => m[1])
  return { text: text.replace(/\s*\[(?:CL|OPT|SRC|EV|FEAT|DEC|PRM|UNK|EXP|SEG|TRG)-\d+\]/g, "").trim(), ids }
}

/** Число с существительным в нужной форме: 1 факт, 2 факта, 5 фактов, 82 факта. */
export function plural(n: number, one: string, few: string, many: string): string {
  const d = n % 10
  const dd = n % 100
  const form = d === 1 && dd !== 11 ? one : d >= 2 && d <= 4 && (dd < 12 || dd > 14) ? few : many
  return `${n} ${form}`
}

export const rub = (v: number) => `${v.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`
export const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`)
