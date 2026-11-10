import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

/** Код строки реестра или узла: CL-007, SRC-002, OPT-001, DEC-0001… */
const CODE = String.raw`[A-Z]{2,6}-\d{3,4}`
/** «[CL-044][CL-045]» в Markdown — ссылка-сноска, а не две ссылки: такие коды и одиночные «[CL-052]» становятся
 * обычными ссылками «#код», как «[CL-029](#CL-029)» в остальном тексте отчёта. */
const BARE = new RegExp(String.raw`\[(${CODE})\](?!\()`, "g")
const HREF = new RegExp(`^#(${CODE})$`)

export function linkCodes(text: string): string {
  return text.replace(BARE, "[$1](#$1)")
}

/** Markdown агента (отчёт, подробности шлюза) — типографикой Tailwind, таблицы прокручиваются внутри.
 * Ссылки на строки реестра — чипами, как в ответе; по клику открывается факт. */
export function Markdown({
  children,
  className,
  onOpenClaim,
}: {
  children: string
  className?: string
  onOpenClaim?: (claimId: string) => void
}) {
  const components: Components = {
    a({ href, children: label, ...rest }) {
      const code = href?.match(HREF)?.[1]
      if (!code) return <a href={href} {...rest}>{label}</a>
      const open = code.startsWith("CL-") && onOpenClaim
      return (
        <Badge
          variant="outline"
          className={cn("mx-0.5 align-middle font-mono text-[11px] font-normal text-muted-foreground no-underline", open && "cursor-pointer")}
          data-testid="claim-chip"
          onClick={open ? () => onOpenClaim(code) : undefined}
        >
          {code}
        </Badge>
      )
    },
  }
  return (
    <div
      className={cn(
        "prose prose-neutral max-w-none prose-headings:font-semibold prose-h2:mt-10 prose-table:block prose-table:overflow-x-auto prose-td:align-top prose-a:text-primary",
        className,
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {linkCodes(children)}
      </ReactMarkdown>
    </div>
  )
}
