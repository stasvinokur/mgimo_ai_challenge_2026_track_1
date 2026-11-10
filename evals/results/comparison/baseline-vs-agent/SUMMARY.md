# Слепое сравнение: baseline против agent

Победа засчитывается, только если вердикт совпал в обоих порядках предъявления; иначе — «ничья/неустойчиво».

| Бриф | Судья | Итог | evidence | epistemics | options | strategy | validation | decision_usefulness |
|---|---|---|---|---|---|---|---|---|
| app-onboarding | mistral-large-4:cloud | agent | tie/unstable | agent | agent | tie/unstable | agent | agent |
| app-onboarding | kimi-k2.6:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| b2b-field-service | mistral-large-4:cloud | agent | agent | agent | agent | tie/unstable | agent | agent |
| b2b-field-service | kimi-k2.6:cloud | agent | agent | tie/unstable | agent | tie/unstable | agent | tie/unstable |
| family-pack | mistral-large-4:cloud | agent | agent | agent | agent | baseline | agent | agent |
| family-pack | kimi-k2.6:cloud | tie/unstable | tie/unstable | tie/unstable | tie/unstable | tie/unstable | tie/unstable | tie/unstable |
| feature-sunset | mistral-large-4:cloud | agent | agent | agent | agent | baseline | agent | tie/unstable |
| feature-sunset | kimi-k2.6:cloud | tie/unstable | agent | agent | agent | baseline | agent | baseline |
| price-increase-a | mistral-large-4:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| price-increase-a | kimi-k2.6:cloud | tie/unstable | agent | tie/unstable | agent | tie/unstable | agent | tie/unstable |
| price-increase-b | mistral-large-4:cloud | agent | tie/unstable | agent | agent | tie/unstable | agent | tie/unstable |
| price-increase-b | kimi-k2.6:cloud | tie/unstable | tie/unstable | agent | agent | tie/unstable | agent | baseline |
| subscription-retention | mistral-large-4:cloud | agent | agent | agent | agent | tie/unstable | agent | tie/unstable |
| subscription-retention | kimi-k2.6:cloud | tie/unstable | agent | agent | agent | tie/unstable | agent | tie/unstable |

Итог по всем вердиктам: baseline — 0, agent — 9, ничья/неустойчиво — 5.
