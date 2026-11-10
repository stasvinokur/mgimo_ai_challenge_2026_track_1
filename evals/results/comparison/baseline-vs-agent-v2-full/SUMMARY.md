# Слепое сравнение: baseline против agent-v2 (полные документы)

Победа засчитывается, только если вердикт совпал в обоих порядках предъявления; иначе — «ничья/неустойчиво».

Документы целиком, без сокращения до общего бюджета: служебные разделы и якоря сняты. Длина, тыс. символов: baseline — 7–12, agent-v2 — 64–80. Вердикт по входу, который модель обрезала по контексту, не засчитывается.

| Бриф | Судья | Итог | evidence | epistemics | options | strategy | validation | decision_usefulness |
|---|---|---|---|---|---|---|---|---|
| app-onboarding | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| app-onboarding | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| b2b-field-service | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable | agent-v2 | tie/unstable |
| b2b-field-service | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| family-pack | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 |
| family-pack | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie | agent-v2 | tie/unstable |
| feature-sunset | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 |
| feature-sunset | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable | agent-v2 | tie/unstable |
| price-increase-a | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| price-increase-a | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie |
| price-increase-b | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| price-increase-b | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | tie/unstable |
| subscription-retention | mistral-large-4:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 |
| subscription-retention | kimi-k2.6:cloud | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 | agent-v2 |

Итог по всем вердиктам: baseline — 0, agent-v2 — 14, ничья/неустойчиво — 0.
