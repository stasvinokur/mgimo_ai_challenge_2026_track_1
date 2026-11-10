import { Download } from "lucide-react"
import { useEffect, useState } from "react"

import { Markdown } from "@/components/markdown"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

export function ReportTab({
  caseId,
  version,
  onOpenFact,
}: {
  caseId: string
  version: number
  onOpenFact: (claimId: string) => void
}) {
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    setText(null)
    api.report(caseId).then(setText, () => setText(""))
  }, [caseId, version])

  const toc = (text ?? "")
    .split("\n")
    .filter((l) => l.startsWith("## "))
    .map((l) => l.slice(3).trim())

  return (
    <div className="mx-auto grid w-full max-w-5xl gap-8 lg:grid-cols-[1fr_14rem]" data-testid="report">
      <div>
        {text === null && <Skeleton className="h-96" />}
        {text !== null && <Markdown onOpenClaim={onOpenFact}>{text}</Markdown>}
      </div>
      <aside className="hidden lg:block">
        <div className="sticky top-6 grid gap-3">
          <Button variant="outline" size="sm" asChild data-testid="report-download">
            <a href={`/api/cases/${caseId}/report`} download={`${caseId}.md`}>
              <Download /> Скачать .md
            </a>
          </Button>
          <nav className="grid gap-1 text-sm text-muted-foreground" data-testid="report-toc">
            {toc.map((h) => (
              <span key={h} className="truncate">{h}</span>
            ))}
          </nav>
        </div>
      </aside>
    </div>
  )
}
