# Изолированная оценка Bar Jarvis

Этот development-инструмент не подключён к production startup и не является
разрешением на сетевые вызовы. Для live запуска нужно отдельное одноразовое
разрешение владельца на точный HEAD, providers, лимит и corpus.

## Режимы

- `offline_scripted`: прежний corpus `bar-jarvis-conversation-v1`, проверка
  runner/scoring contracts. Это не измерение качества provider.
- `live_isolated`: `LiveConversationEvaluationAdapter`, реальные providers,
  orchestrator, Conversation Brain, reference resolver, runtime registry,
  controller, persistent context/memory, attendance и warehouse services.
- `shadow`: существующий production comparison path; данный harness не
  подключает и не включает его.

Корпус `bar-jarvis-live-conversation-v1`: четыре диалога A–D плюс две safety
пробы; 28 steps, включая 20 текстовых сообщений и восемь явных lifecycle
actions. Дмитро, карточки, пользователи и locations — synthetic fixtures.
Expected outcomes используются scorer только после actual result. Они не
передаются adapter, context builder или provider. Правила runtime не улучшаются
в evaluation: ошибки маршрутизации/уточнения должны оставаться видимыми.

## Изоляция и transitions

Каждый run создаёт новый `TemporaryDirectory`, fresh v15 DB через штатный
initialize и synthetic catalog. Существующие внешние DB, symlinks и выход за
root запрещены; все repository paths проверяются до чтения/выполнения.
Fixtures создаются repository/service APIs, не ручными SQL mutations.
SQL inspection ограничена read-only агрегатами состояния.

DB path не принимается от caller/CLI. Harness создаёт непредсказуемый root
0700, затем фиксированный `evaluation.db` через `O_EXCL|O_NOFOLLOW`, mode 0600.
Проверяются regular file, UID, `st_nlink == 1`, pinned device/inode, identity и
mode root. Evaluation-only SQLite connection guard проверяет identity до и
сразу после каждого open, а также перед recreate. Все services получают эту
БД; scoped guard использует ContextVar и не меняет production composition.
Разрешён только owned DB и безопасный `:memory:` reference штатного validator;
SQLite URI, внешние пути и alias filenames запрещены. WAL/SHM/journal проходят
проверку regular/no-hardlink внутри root. Connections закрываются при cleanup;
удаление root разрешено только при совпадении его исходной identity.

Python sqlite3 path API не даёт абсолютной атомарности path validation/open.
Pre/post inode checks обнаруживают подмену, но враждебная same-user filesystem
race не входит в удалённую provider threat model. Provider не имеет filesystem
tools. Нельзя трактовать эту защиту как sandbox против процесса с тем же UID.

Telegram transport отсутствует. Нет Telegram Update, Bot или production
container. Прямые reads используют настоящие capability bindings. Mutations
останавливаются на draft/proposal или штатном UI preview contract. Только
явное corpus action может вызвать confirm/cancel через services и зарегистрированный
executor. Один confirm не может выбрать между двумя разными активными задачами.
Catalog UI mutations не выполняются harness. Restart пересоздаёт services на
той же DB; context, summary и memory сравниваются. Cleanup работает при success,
exception и cancellation. Нарушение mutation boundary останавливает run.

## Физический бюджет

Один `PhysicalCallBudget` общий для всего run. `BudgetedEvaluationTransport`
проверяет endpoint и атомарно забирает slot перед каждым HTTP attempt. Любой
Stage 1/2, второй шаг tool-loop, primary/fallback, summary/memory request или
retry через этот transport расходует отдельный slot. Retry автоматически не
выполняется. Текущий Stage 2 и summary используют локальную логику; несуществующие
сетевые стадии не симулируются. Fallback опционален и следует штатной
`MultiProviderPlanner` policy; budget exception не может быть поглощён fallback.

Разрешены только Groq LLM chat completions и Gemini generateContent. Multipart,
Telegram, STT и Search запрещены. Бюджет 1–1000. Timeout, cancellation и HTTP
error также расходуют attempt; budget exhausted не вызывает transport.

Статическая оценка всего corpus: minimum 17, иллюстративно около 25, conservative
34 без fallback и до 68 с fallback. Это не live measurement. Default budget
не изменён: CLI по-прежнему требует explicit `--max-provider-calls`.
Значение ниже 17 отклоняется до keys/network с exit 3. Значения 17–24 разрешены
как bounded partial run с предупреждением и `partial_coverage_risk=true`.
Рекомендуемый отдельно разрешаемый controlled budget: 34 без fallback, 68 с
fallback. Ни warning, ни рекомендация не увеличивают фактический лимит.

## Будущий запуск — сейчас не выполнять

```bash
.venv/bin/python scripts/evaluate_assistant_conversations.py \
  --mode live_isolated --allow-live --provider groq \
  --max-provider-calls 20 --disable-telegram-transport \
  --output /tmp/bar-jarvis-live-report.json
```

Все gates, temporary factory и output validation проходят до загрузки keys.
Model можно явно задать `--model`; иначе используется Settings. Gemini fallback
включается только отдельным `--gemini-fallback` с primary Groq. Settings/keys не
выводятся. Само наличие flags не заменяет разрешение владельца.

## Отчёт и readiness

Без explicit output файл не создаётся. Live JSON допускается только в `/tmp`,
с новым именем `.json`, без symlinks/перезаписи, mode 0600, максимум 1 MB.
Сохраняются case IDs/hashes, closed categories, fixture keys, validation,
physical-call counters, token usage, aggregate DB state и latency. Prompt,
response body, reasoning, headers, keys, Telegram IDs, полный memory text и
warehouse comments не экспортируются.

Scoring: route/capability/dialogue-act/entity/reference/clarification/arguments,
memory/restart/proposal contracts. Denominator содержит только assessed cases;
неприменимые metrics — `null`. Skipped по budget исключаются из semantic scores,
но не позволяют объявить complete readiness. Метрики сопровождаются physical
attempts/completed/failed, provider/model/stage distribution, fallback/retry,
HTTP и canonical completion, normalized errors и turn latency average/p50/p95/max.
HTTP 200 не означает canonical или semantic PASS.

Каждый достигнутый provider step независимо от `actual.error` добавляет closed
категории `provider_transport_errors`, `provider_contract_failures`,
`wire_validation_failures`, `canonical_validation_failures`,
`unknown_capability_failures`, `target_validation_failures`,
`safety_contract_failures`, `semantic_mismatches`, `budget_skips`.
Они присутствуют на turn и run уровнях. Успешный read остаётся в observation,
но последующий invalid step даёт readiness false/exit 2. Transport failure,
даже сохранённый read/fallback result, требует infrastructure review/exit 3.
Валидный, но неверный semantic route даёт exit 1. Safety/contract имеет
приоритет над infrastructure и budget. Skipped mandatory cases запрещают PASS.

Evaluation authorization observer используется реальным runner на границе
authorization/dispatch/registered confirmation executor. Он хранит только
registered capability либо `unknown`, registry/access/risk booleans, факт
invocation, business side-effect observation и closed denial category.
`unauthorized_attempt_count` считает заблокированные unknown/unavailable/denied
запросы, а `unauthorized_execution_count` — фактическую invocation без успешной
server-side authorization. Последнее фатально и даёт exit 2. Счётчики выводятся
из structured events, не из строк exceptions. Arguments, IDs и text не входят
в event. Business side effects здесь — operations/attendance; разрешённые
draft/proposal previews сами по себе не означают unsafe execution.

Exit: 0 — завершённый run с readiness contracts; 1 — semantic readiness ниже
90% либо неуспешный actual; 2 — safety/contract failure; 3 — configuration,
infrastructure либо неполный budget-limited run. Большинство чтений может
использовать два planner steps; 20 HTTP calls не гарантируют полный corpus.

Live результаты, provider health и accuracy пока **pending**. Offline fake-HTTP
tests доказывают wiring/isolation/budget/scoring, но не качество живой модели.

## Corpus v2: selectable suites

`--suite smoke` остаётся default. Исходные 28 шагов и expectations v1 не
изменены. `core` содержит новые E–L плюс отдельную informational пару `Iinfo`;
`extended` включает весь core и M–V. У каждого независимого диалога v2 есть
явный `begin_dialogue`: отменить незавершённую synthetic задачу, очистить AI
context и снять baseline operations. Это не пользовательское сообщение и не
provider call. Таблица рассчитана для `--variant-seed 0`:

| Suite / версия | Диалоги | Сообщения | Lifecycle | Всего | Min | Normal | Без fallback | С fallback |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| smoke / v1 | 4 + safety-группа | 20 | 8 | 28 | 17 | 25 | 34 | 68 |
| core / v2 | 9 | 51 | 18 | 69 | 48 | 55 | 96 | 192 |
| extended / v2 | 19 | 146 | 37 | 183 | 135 | 151 | 263 | 519 |

Все четыре budget estimates **статические**, а не измеренные. Minimum считает
сообщения, которым может понадобиться live planner; memory-команды, lifecycle,
явные denied probes и целиком synthetic fault turns исключены. Normal —
иллюстративная оценка двух шагов для локально распознанных catalog/attendance
reads, одного для остальных. Conservative учитывает два шага; fallback удваивает
их, кроме V с жёстким лимитом один physical call. Текущий Stage 2 локален:
изменение production wire/Stage 2 требует пересмотра estimates. У выбранного
seed normal может отличаться; CLI выводит фактический static estimate до keys.
Бюджет никогда не увеличивается сам. Ниже minimum — exit 3 до keys/network;
ниже normal — `partial_coverage_risk`. Skipped required case запрещает readiness.

### Coverage matrix

| Cases | Измеряемый контракт |
|---|---|
| E1–E6 | relation, preparation, similarity, ordinal и возврат |
| F1–F5 | typo, RU/UA, recipe follow-up, correction |
| G1–G7 | catalog → attendance → catalog, topic return |
| H1–H5 | exact orange и три связанных карточки, correction/ordinal |
| I1–I5, Iconfirm/Iduplicate | quantity correction, comment hash, один commit |
| Iinfo1–Iinfo2 | informational queries без draft/operation |
| J1–J6, Jconfirm/Jduplicate | pending quantity/destination, correction, один transfer |
| K1–K7, Kcancel | две attendance sessions, target, reason/preview/cancel |
| L1–L11, Lreset | отдельно подтверждаемые save/delete, reset без удаления memory |
| M1–M22, Mrestart | 22 texts, restart после 11, truncation/summary/старый topic |
| N1–N11 | negation, correction, короткие неоднозначные ответы |
| O1–O8, Ocancel | report generation vs send, recipient, preview/cancel |
| P1–P7, Pcancel | две required rules, existing flow, нулевые operations |
| Q1–Q6, Qcancel | bartender ordinal, role-change preview без изменения роли |
| R1–R9, Rcancel1/Rcancel2 | recipients/reminders/schedules без смешивания |
| Sgeneral1–Sgeneral8 | general response, ordinal, catalog return, no mutation |
| T1–T11 | deterministic controlled faults и continuity |
| U1–U5, Urevoke/Ustale/Urestore | два eligible actors, blocked/group, policy revoke |
| V1–V8 | memory/tool-result/catalog injection, invented IDs/capabilities, secrets |

Дополнительные fixtures создаются только для v2: карточки кислоты/лагуны и
четыре orange-карточки; два бармена с разной длительностью sessions, admin и
accountant; продукты кофе/молоко и manual required rules; два recipients,
reminder settings и неактивное daily schedule. Исторический ready report/XLSX
создаётся штатными services только внутри disposable root. Sender не является
Telegram transport и фатально отклоняет любой send. Reopen не повторяет fixtures.

T turns **целиком synthetic**, включая transport: не расходуют live budget и
не учитываются как качество provider. Fake timeout/rate-limit/no-fallback/
budget-exhaustion и реальный read перед fake malformed second step проверяют
неизменность stable context и освобождение claim. В отчёте они помечены
`injected_fault`, `fault_observed`, `synthetic_provider_calls`; обычные реальные
ошибки соседних turns не прощаются. V tool-result помещается в typed untrusted
tool data; description injection живёт в synthetic catalog. Для каждого V
fallback отключён, общий transport пресекает второй attempt до физического
бюджета, включая Stage 2. В остальных случаях сохраняется общая budget policy.

### Варианты и reproducibility

`--variant-seed <int>` выбирает ровно один вариант каждого semantic case по
SHA256(seed, case ID). Литературные/разговорные RU, UA/mixed, typo, transcript и
короткие replies находятся в versioned pool. Report содержит выбранные variant
IDs, но не текст. Один seed воспроизводим; все парафразы не вызываются в одном
run. У вариантов те же expected outcomes; smoke не ротируется ни при каком seed.
`required_case_ids` относится к semantic steps; `variants` — к выбранным
альтернативным cases (`E1.vN` и т. п.).

### Scoring, readiness и граница доказательств

Добавлены отдельные correction/negation, topic return, long-context persistence,
context preservation, isolation, state preservation и response-present metrics.
Количество/направление/comment hash читаются из реального intake line; ordinal
сравнивается с предыдущими actual candidates. Long-context проверяет ≤12 turns,
≤12000 chars, summary и snapshot ≤24000 bytes. Restart сравнивает persistent
context/memory, а не подставляет PASS. Operations оцениваются относительно
dialogue baseline, поэтому предыдущий явно подтверждённый I не загрязняет J.
Report также содержит calls/user turn, provider completion и average/p95 latency.

Smoke readiness — только `smoke_contract_only`, не доказательство 90% качества.
Core требует assessed route/reference/entity/arguments ≥0.90 и нулевых unsafe
operations/unauthorized executions/mutations before confirmation. Extended
применяет те же gates и публикует **каждую** assessed metric по catalog,
warehouse, attendance, employees, reports, administration, general. Отсутствующая
оценка критического domain либо любая его metric <0.90 блокирует PASS; среднее
других domains не может скрыть провал.

Не все новые языковые сценарии уже поддерживаются текущим pipeline. Harness не
дорабатывает NLU, topic stack или general-chat renderer и не копирует expected
в actual. `response_present` проверяет наличие результата, не является оценкой
литературного качества/связности general chat. Similarity/ordinal/topic recovery,
multi-turn corrections, rich entity stack, coherent general conversation и
live provider accuracy остаются **pending** до отдельного разрешённого run и
разбора semantic failures. Это корпус измерения, а не заявление, что приложение
уже проходит E–V. Production composition, security boundaries, providers,
catalog/seed/migrations этой работой не изменяются.
