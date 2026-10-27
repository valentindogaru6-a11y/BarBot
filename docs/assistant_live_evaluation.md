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
