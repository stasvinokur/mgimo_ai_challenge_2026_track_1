import { Badge } from "@/components/ui/badge"
import { splitClaims } from "@/lib/api"

/** Текст без служебных кодов и строки реестра чипами: по клику открывается факт во вкладке «Факты». */
export function ClaimText({
  text,
  claimIds = [],
  onOpen,
}: {
  text: string
  claimIds?: string[]
  onOpen?: (claimId: string) => void
}) {
  const parts = splitClaims(text)
  const ids = [...new Set([...parts.ids, ...claimIds])]
  return (
    <span>
      {parts.text}
      {ids.map((id) => (
        <Badge
          key={id}
          variant="outline"
          className="ml-1.5 cursor-pointer align-middle font-mono text-[11px] text-muted-foreground"
          data-testid="claim-chip"
          onClick={() => onOpen?.(id)}
        >
          {id}
        </Badge>
      ))}
    </span>
  )
}
