# Слепое сравнение: baseline против agent (полные документы)

Победа засчитывается, только если вердикт совпал в обоих порядках предъявления; иначе — «ничья/неустойчиво».

Документы целиком, без сокращения до общего бюджета: служебные разделы и якоря сняты. Длина, тыс. символов: baseline — 7–12, agent — 69–82. Вердикт по входу, который модель обрезала по контексту, не засчитывается.

| Бриф | Судья | Итог | evidence | epistemics | options | strategy | validation | decision_usefulness |
|---|---|---|---|---|---|---|---|---|
| app-onboarding | mistral-large-4:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| app-onboarding | kimi-k2.6:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| b2b-field-service | mistral-large-4:cloud | tie/unstable | tie/unstable | agent | agent | tie/unstable | agent | tie/unstable |
| b2b-field-service | kimi-k2.6:cloud | agent | agent | agent | agent | agent | agent | tie/unstable |
| family-pack | mistral-large-4:cloud | agent | agent | agent | agent | baseline | agent | tie |
| family-pack | kimi-k2.6:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| feature-sunset | mistral-large-4:cloud | agent | agent | agent | agent | tie/unstable | agent | agent |
| feature-sunset | kimi-k2.6:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| price-increase-a | mistral-large-4:cloud | agent | agent | agent | agent | agent | agent | agent |
| price-increase-a | kimi-k2.6:cloud | agent | agent | agent | agent | agent | agent | tie/unstable |
| price-increase-b | mistral-large-4:cloud | agent | agent | agent | agent | agent | agent | tie/unstable |
| price-increase-b | kimi-k2.6:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| subscription-retention | mistral-large-4:cloud | agent | agent | agent | agent | agent | agent | tie/unstable |
| subscription-retention | kimi-k2.6:cloud | agent | agent | agent | agent | agent | agent | tie/unstable |

Итог по всем вердиктам: baseline — 0, agent — 13, ничья/неустойчиво — 1.
