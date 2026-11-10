import { useCallback, useEffect, useState } from "react"

import { CaseHeader } from "@/components/case-header"
import { CaseSidebar } from "@/components/case-sidebar"
import { DecisionTab } from "@/components/decision-tab"
import { FactsTab } from "@/components/facts-tab"
import { ReportTab } from "@/components/report-tab"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useCase } from "@/hooks/use-case"
import { api, type CaseItem } from "@/lib/api"

type Tab = "decision" | "facts" | "report"

/** Адрес страницы: #/<кейс>/<вкладка> — панель открывается там же после обновления. */
function readHash(): { caseId: string | null; tab: Tab } {
  const [, caseId, tab] = window.location.hash.split("/")
  return { caseId: caseId || null, tab: (["decision", "facts", "report"].includes(tab) ? tab : "decision") as Tab }
}

export default function App() {
  const [cases, setCases] = useState<CaseItem[]>([])
  const [demo, setDemo] = useState(false)
  const [{ caseId, tab }, setRoute] = useState(readHash)
  const [fact, setFact] = useState<string | null>(null)
  const { data, error, live, reload } = useCase(caseId)

  const loadCases = useCallback(async () => {
    const list = await api.cases()
    setCases(list)
    return list
  }, [])

  useEffect(() => {
    void api.meta().then((m) => setDemo(m.demo))
    void loadCases().then((list) => {
      if (!readHash().caseId && list.length) go(list[0].id, "decision")
    })
    const onHash = () => setRoute(readHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [loadCases])

  function go(id: string, next: Tab = tab) {
    window.location.hash = `/${id}/${next}`
  }

  function openFact(claimId: string) {
    setFact(claimId)
    if (caseId) go(caseId, "facts")
  }

  return (
    <SidebarProvider>
      <CaseSidebar
        cases={cases}
        current={caseId}
        demo={demo}
        onSelect={(id) => go(id, "decision")}
        onCreated={(id) => {
          void loadCases()
          go(id, "decision")
        }}
      />
      <SidebarInset>
        {!caseId && <Empty demo={demo} />}
        {caseId && error && <p className="p-8 text-destructive">{error}</p>}
        {caseId && !data && !error && <Skeleton className="m-8 h-64" />}
        {data && (
          <>
            <CaseHeader data={data} demo={demo} onChanged={reload} />
            <Tabs value={tab} onValueChange={(v) => go(data.id, v as Tab)} className="gap-6 px-6 py-5">
              <TabsList data-testid="tabs">
                <TabsTrigger value="decision" data-testid="tab-decision">Решение</TabsTrigger>
                <TabsTrigger value="facts" data-testid="tab-facts">Факты</TabsTrigger>
                <TabsTrigger value="report" data-testid="tab-report" disabled={!data.finished}>Отчёт</TabsTrigger>
              </TabsList>
              <TabsContent value="decision">
                <DecisionTab data={data} live={live} demo={demo} onChanged={reload} onOpenFact={openFact} />
              </TabsContent>
              <TabsContent value="facts">
                <FactsTab caseId={data.id} version={data.version} open={fact} onOpenChange={setFact} onContested={() => {
                  go(data.id, "decision")
                  void reload()
                }} />
              </TabsContent>
              <TabsContent value="report">
                <ReportTab caseId={data.id} version={data.version} onOpenFact={openFact} />
              </TabsContent>
            </Tabs>
          </>
        )}
      </SidebarInset>
    </SidebarProvider>
  )
}

function Empty({ demo }: { demo: boolean }) {
  return (
    <div className="m-auto grid max-w-md gap-3 p-8 text-center" data-testid="empty">
      <h1 className="text-2xl font-semibold">Стратег Фабрики проектов</h1>
      <p className="text-muted-foreground">
        Опишите задачу своими словами и приложите материалы — агент соберёт факты с источниками, предложит варианты и
        поможет принять решение, которое можно проверить.
      </p>
      <p className="text-sm text-muted-foreground">
        {demo ? "Слева — «Открыть демо» или «Новый кейс»." : "Слева — «Новый кейс»."}
      </p>
    </div>
  )
}
