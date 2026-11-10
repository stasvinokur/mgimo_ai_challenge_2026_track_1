# Сравнение: простой чат против агента

Одни и те же 7 брифов и материалы, одна reasoning-модель. Метрики считает судья другого семейства
(`evals/metrics.py`, 20 случайных утверждений на бриф); для агента оценивается стратегический документ без
служебных разделов (самоаудит, чек стоимости, приложение-реестр). Это автоматический прокси, а не оценка
эксперта: нужна выборочная ручная проверка.

| Бриф | Подход | Без опоры | Противоречит | «Оставить» | «Закрыть» | Неподкр. обещания | Baseline | Guardrail | Kill с датой | Как у эксперта | Вызовов | $ | Время, с |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| app-onboarding | baseline | 0% | 0 | нет | да | 6 из 9 | да | нет | нет | yes | 1 | 0.0047 | 14.9 |
| app-onboarding | agent | 10% | 0 | да | да | 0 из 10 | да | да | да | yes | 160 | 0.1099 | 236.6 |
| b2b-field-service | baseline | 0% | 0 | нет | нет | 1 из 11 | да | нет | нет | yes | 1 | 0.0044 | 12.9 |
| b2b-field-service | agent | 10% | 0 | да | да | 0 из 9 | да | да | да | partial | 180 | 0.1246 | 240.5 |
| family-pack | baseline | 10% | 1 | да | да | 0 из 7 | да | да | да | yes | 1 | 0.0114 | 20.5 |
| family-pack | agent | 15% | 0 | да | да | 0 из 8 | да | да | да | yes | 178 | 0.1241 | 245.5 |
| family-pack | agent-local | 25% | 3 | да | да | 3 из 9 | нет | да | да | no | 112 | 0.0000 | 1132.5 |
| feature-sunset | baseline | 5% | 1 | да | да | 0 из 10 | да | да | да | yes | 1 | 0.0052 | 16.1 |
| feature-sunset | agent | 5% | 0 | да | да | 0 из 8 | да | да | да | yes | 145 | 0.1049 | 194.6 |
| price-increase-a | baseline | 15% | 1 | нет | нет | 2 из 3 | да | да | да | yes | 1 | 0.0045 | 15.2 |
| price-increase-a | agent | 10% | 1 | да | нет | 0 из 12 | да | да | да | yes | 146 | 0.1107 | 201.6 |
| price-increase-a | agent-local | 15% | 1 | да | да | 9 из 10 | да | да | да | yes | 85 | 0.0000 | 597.4 |
| price-increase-b | baseline | 0% | 0 | нет | нет | 5 из 14 | да | да | да | partial | 1 | 0.0060 | 17.8 |
| price-increase-b | agent | 10% | 1 | да | да | 0 из 6 | да | да | да | yes | 164 | 0.1225 | 231.8 |
| subscription-retention | baseline | 10% | 1 | нет | нет | 9 из 14 | да | нет | нет | yes | 1 | 0.0079 | 13.8 |
| subscription-retention | agent | 30% | 4 | да | да | 4 из 8 | да | да | да | yes | 155 | 0.1276 | 191.2 |
| subscription-retention | agent-local | 20% | 0 | да | да | 10 из 10 | да | да | да | partial | 73 | 0.0000 | 1632.1 |

| Подход | «Оставить» | «Закрыть» | Guardrail | Kill с датой | Выбор как у эксперта |
|---|---|---|---|---|---|
| baseline | 2/7 | 3/7 | 4/7 | 4/7 | 6/7 |
| agent | 7/7 | 6/7 | 7/7 | 7/7 | 6/7 |
| agent-local | 3/3 | 3/3 | 3/3 | 3/3 | 1/3 |

## Слепое попарное судейство

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

### Третий судья: Codex (GPT-6.1), вместо ручной проверки

Ручной проверки людьми нет. Судьи стенда видели документ агента сокращённым до общего бюджета длины:
из 71–84 тыс. символов оставалось 15–17 тыс., а ответ чата помещался целиком. Codex оценил те же пары
вслепую в обоих порядках и с материалами брифа:

- те же сокращённые документы, что у судей стенда: baseline — 4, agent — 2, ничья/неустойчиво — 1 из 7. (`baseline-vs-agent-codex/SUMMARY.md`)
- полные документы: baseline — 3, agent — 3, ничья/неустойчиво — 1 из 7. (`baseline-vs-agent-codex-full/SUMMARY.md`)
- полные документы, агент v0.2.0 (`agent-v2`): baseline — 2, agent-v2 — 5, ничья/неустойчиво — 0 из 7. (`baseline-vs-agent-v2-codex-full/SUMMARY.md`)

В чью пользу ошибались судьи (опровергнутые утверждения): агента — 82, чата — 9, неясно — 5.

## Decision Reversal

1 новых источника → 31 новых строк реестра → 3 оснований под вопросом → 16 выводов пересмотрено, 67 узлов не затронуто; решение DEC-0001 открыто заново. Токенов на пересмотр: 86269 против 231590 на полную перегенерацию.

Повторные прогоны (3): решение открыто заново — 3, рекомендация ушла от неприемлемой — 3, совпала с экспертом — 1 (`reversal_runs.json`).

## Дешёвый маршрут: всё на локальной gpt-oss:20b

Один и тот же код агента, одни брифы: облачные модели ролей (`agent-cloud`) против профиля
`STRATEGIST_MODELS_PROFILE=local` (`agent-local`, все роли и decide() на gpt-oss:20b, без облака).

| Бриф | Маршрут | Без опоры | «Оставить» | «Закрыть» | Неподкр. обещания | Guardrail | Kill с датой | Как у эксперта | Вызовов | $ | Время, мин |
|---|---|---|---|---|---|---|---|---|---|---|---|
| family-pack | agent-cloud | 15% | да | да | 2 из 9 | да | да | yes | 160 | 0.0971 | 4.7 |
| family-pack | agent-local | 25% | да | да | 3 из 9 | да | да | no | 112 | 0.0000 | 18.9 |
| price-increase-a | agent-cloud | 30% | да | да | 0 из 15 | да | да | yes | 142 | 0.0959 | 4.7 |
| price-increase-a | agent-local | 15% | да | да | 9 из 10 | да | да | yes | 85 | 0.0000 | 10.0 |
| subscription-retention | agent-cloud | 40% | да | да | 3 из 9 | да | да | yes | 148 | 0.1312 | 4.8 |
| subscription-retention | agent-local | 20% | да | да | 10 из 10 | да | да | partial | 73 | 0.0000 | 27.2 |

### Слепое попарное судейство облака и локального маршрута

Победа засчитывается, только если вердикт совпал в обоих порядках предъявления; иначе — «ничья/неустойчиво».

| Бриф | Судья | Итог | evidence | epistemics | options | strategy | validation | decision_usefulness |
|---|---|---|---|---|---|---|---|---|
| family-pack | mistral-large-4:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |
| family-pack | kimi-k2.6:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |
| price-increase-a | mistral-large-4:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |
| price-increase-a | kimi-k2.6:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |
| subscription-retention | mistral-large-4:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |
| subscription-retention | kimi-k2.6:cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud | agent-cloud |

Итог по всем вердиктам: agent-cloud — 6, agent-local — 0, ничья/неустойчиво — 0.

## Перепись утверждений: доля без опоры

Проверены все утверждения о фактах, а не выборка из 20 (`evals/metrics.py --sample 0 --claims-only`, судья
Mistral, документ режется по разделам). Доля «по сумме» — все утверждения подхода вместе, интервал Уилсона
(утверждения одного документа не независимы, поэтому он узковат). «Средняя по брифам» — каждый бриф с одним
весом, интервал — бутстреп по брифам (10 000 повторов): он учитывает разброс между задачами.

| Подход | Брифов | Утверждений | Без опоры | Доля по сумме | 95% Уилсон | Средняя по брифам | 95% бутстреп |
|---|---|---|---|---|---|---|---|
| `agent` | 7 | 1275 | 265 | 20,8% | 18,6%–23,1% | 20,4% | 16,8%–24,4% |
| `agent-cloud` | 3 | 600 | 140 | 23,3% | 20,1%–26,9% | 23,8% | 16,7%–30,9% |
| `agent-v2` | 7 | 1301 | 265 | 20,4% | 18,3%–22,6% | 21,0% | 14,4%–26,6% |
| `agent-v2-off` | 7 | 1338 | 211 | 15,8% | 13,9%–17,8% | 16,3% | 12,3%–21,2% |
| `agent-v2-off-r2` | 7 | 1176 | 241 | 20,5% | 18,3%–22,9% | 21,0% | 16,1%–26,3% |
| `agent-v2-r2` | 7 | 1277 | 193 | 15,1% | 13,3%–17,2% | 14,9% | 10,9%–20,4% |
| `baseline` | 7 | 453 | 70 | 15,5% | 12,4%–19,1% | 15,1% | 10,5%–19,0% |
| `baseline-r2` | 7 | 441 | 57 | 12,9% | 10,1%–16,4% | 12,6% | 9,3%–15,8% |
| `baseline-r3` | 7 | 475 | 82 | 17,3% | 14,1%–20,9% | 17,1% | 14,2%–20,2% |

Разность с чатом на общих брифах (повторные прогоны чата сложены; бутстреп по брифам, парный). Строки
«без стресс-теста» не считают раздел «Стресс-тест решения»: в v0.2.0 он подписан как расчёт модели агента
по допущениям, а не данные из материалов, но судья переписи всё равно считает его числа утверждениями без
опоры. Обе строки — честные: первая — документ целиком, вторая — без раздела, помеченного как модель.

| Подход | Брифов | Доля подхода | Доля чата | Разность, п.п. | 95% бутстреп | Вывод |
|---|---|---|---|---|---|---|
| `agent` | 7 | 20,8% | 15,3% | +5,5 | +1,0…+9,7 | у подхода больше |
| `agent-cloud` | 3 | 23,3% | 16,1% | +7,2 | -0,1…+13,6 | не различимо |
| `agent-v2` | 7 | 17,8% | 15,3% | +2,5 | -1,6…+7,1 | не различимо |
| `agent-v2-off` | 7 | 18,0% | 15,3% | +2,7 | -0,9…+7,1 | не различимо |
| `agent` без стресс-теста | 7 | 16,5% | 15,3% | +1,3 | -3,9…+5,7 | не различимо |
| `agent-cloud` без стресс-теста | 3 | 20,7% | 16,1% | +4,6 | -2,7…+10,9 | не различимо |
| `agent-v2` без стресс-теста | 7 | 15,7% | 15,3% | +0,5 | -3,5…+5,2 | не различимо |
| `agent-v2-off` без стресс-теста | 7 | 14,6% | 15,3% | -0,7 | -4,4…+3,8 | не различимо |

Где у агента утверждения без опоры — по разделам документа (доля, без опоры / проверено):

| Раздел | `agent` | `agent-cloud` | `agent-v2` | `agent-v2-off` |
|---|---|---|---|---|
| Варианты | 22,5% (113/502) | 20,5% (42/205) | 15,9% (156/981) | 16,2% (147/906) |
| Стресс-тест решения | 100,0% (65/65) | 100,0% (20/20) | 100,0% (62/62) | 99,0% (100/101) |
| План проверки | 60,4% (32/53) | 61,1% (22/36) | 58,8% (87/148) | 60,9% (70/115) |
| Коммуникационная стратегия | 12,7% (14/110) | 18,5% (10/54) | 20,6% (58/282) | 17,1% (54/315) |
| Продуктовая стратегия | 11,0% (15/136) | 7,0% (4/57) | 11,5% (30/261) | 11,7% (31/265) |
| Картина ситуации | 3,8% (9/240) | 18,6% (27/145) | 2,8% (15/535) | 2,2% (11/491) |
| Журнал решения | 10,3% (7/68) | 8,3% (3/36) | 18,7% (26/139) | 13,0% (16/123) |
| Решение на одной странице | 14,5% (9/62) | 23,5% (8/34) | 9,3% (10/107) | 11,3% (12/106) |
| Источники | 2,7% (1/37) | 30,8% (4/13) | 22,6% (14/62) | 11,6% (10/86) |
| Прогнозы (для проверки по Brier) | 0,0% (0/2) | — | 0,0% (0/1) | 16,7% (1/6) |

## Судьи стенда на полных документах

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

## Судьи стенда на полных документах: агент v0.2.0 (`agent-v2`)

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

## Тест смены решения по выжимке (`agent-v2-r2`, n = 7)

Clef выбирает рекомендованный вариант по полному документу, по нашей выжимке и по выжимке одним проходом
(рабочая модель без проверки чисел и опор). Шум — перестановки порядка вариантов на полном документе.

| Сравнение | Совпало |
|---|---|
| Полный документ: выбор не зависит от порядка вариантов | 7 из 7 |
| Наша выжимка → тот же выбор, что по документу | 7 из 7 |
| Выжимка одним проходом → тот же выбор, что по документу | 7 из 7 |
| Полный документ → вариант, выбранный в кейсе | 7 из 7 |
| Наша выжимка → вариант, выбранный в кейсе | 7 из 7 |
| Выжимка одним проходом → вариант, выбранный в кейсе | 7 из 7 |

| Текст | Средняя уверенность Clef | Наименьшая |
|---|---|---|
| Полный документ | 0,97 | 0,96 |
| Наша выжимка | 0,89 | 0,80 |
| Выжимка одним проходом | 0,90 | 0,81 |

Иллюстрация, а не доказательство: n = 7, один судья решений (Clef). Тест проверяет, что выжимка передаёт
ту же рекомендацию, что документ: Clef спрашивают, что рекомендует текст, а рекомендация в нём названа прямо.
Поэтому высокое совпадение ожидаемо и не значит, что по выжимке руководитель сам пришёл бы к тому же
выбору; искажение рекомендации тест поймал бы.

## Тест смены решения по выжимке (`agent-v2`, n = 7)

Clef выбирает рекомендованный вариант по полному документу, по нашей выжимке и по выжимке одним проходом
(рабочая модель без проверки чисел и опор). Шум — перестановки порядка вариантов на полном документе.

| Сравнение | Совпало |
|---|---|
| Полный документ: выбор не зависит от порядка вариантов | 7 из 7 |
| Наша выжимка → тот же выбор, что по документу | 7 из 7 |
| Выжимка одним проходом → тот же выбор, что по документу | 7 из 7 |
| Полный документ → вариант, выбранный в кейсе | 7 из 7 |
| Наша выжимка → вариант, выбранный в кейсе | 7 из 7 |
| Выжимка одним проходом → вариант, выбранный в кейсе | 7 из 7 |

| Текст | Средняя уверенность Clef | Наименьшая |
|---|---|---|
| Полный документ | 0,98 | 0,97 |
| Наша выжимка | 0,94 | 0,89 |
| Выжимка одним проходом | 0,91 | 0,80 |

Иллюстрация, а не доказательство: n = 7, один судья решений (Clef). Тест проверяет, что выжимка передаёт
ту же рекомендацию, что документ: Clef спрашивают, что рекомендует текст, а рекомендация в нём названа прямо.
Поэтому высокое совпадение ожидаемо и не значит, что по выжимке руководитель сам пришёл бы к тому же
выбору; искажение рекомендации тест поймал бы.
