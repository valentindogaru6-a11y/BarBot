# BarBot repository policy

Работать только внутри `/opt/barbot`.

## Default development mode

Для обычной разработки, тестирования, рефакторинга и документации запрещено:

- открывать настоящий `.env`;
- читать или изменять production SQLite, audit-log и XLSX;
- запускать production polling/systemd;
- применять production migrations/import или выполнять deployment.

Тесты используют только временные каталоги/`tmp_path` и fake transports.
Секреты и реальные Telegram ID нельзя выводить ни в одном режиме.

## Explicit controlled production rollout mode

Ограниченные production-действия разрешены только для явно названной задачи
`Phase 10B — Controlled production rollout` или явного продолжения конкретного
`Stage/STOP` этой фазы и только когда одновременно:

1. задание содержит точный подтверждённый Git HEAD;
2. working tree clean;
3. предыдущий stop point пройден;
4. пользователь отдельно и однозначно разрешил именно следующий Stage;
5. действия соответствуют `docs/production_readiness.md`,
   `docs/production_rollout.md` и `docs/production_rollback.md`.

Разрешение одного Stage не переносится на следующий.

### Stage 0 — read-only inspection

После явного задания Phase 10B разрешено:

- программно проверить наличие и формат allowlisted keys настоящего `.env`;
- определить нормализованные production paths;
- открыть production SQLite строго read-only для schema version,
  агрегированных counts, integrity/FK и состояния jobs;
- проверить metadata production audit/report paths;
- выполнить read-only systemd/process inspection.

Allowlist: `BOT_TOKEN`, `SUPER_ADMIN_IDS`, `ADMIN_IDS`, `CATALOG_PATH`,
`DATABASE_PATH`, `WAREHOUSE_REPORTS_PATH`, `WAREHOUSE_ENABLED`. Дополнительный
key можно читать только если configuration code подтверждает, что он необходим
для preflight.

Настоящий `.env` проверяется только программно. Запрещены `cat`, `sed`, `grep`,
shell tracing и другие способы, способные раскрыть значения; нельзя копировать
`.env`. Для чувствительных keys выводится только `configured`, `missing` или
`invalid`, без длины/фрагментов значения. Нельзя выводить реальные Telegram ID
или персональные данные. Stage 0 не разрешает изменять DB/permissions,
останавливать или запускать service, создавать backup либо отправлять сообщения.
Логический SQLite read-only (`mode=ro`, `query_only=ON`) не гарантирует
отсутствие filesystem side effects: SQLite может создать или обновить runtime
`-wal`/`-shm`. Их нельзя удалять либо подменять при работающем service.

### Stage 1 — stop and verified backup

Только после отдельного подтверждения Stage 1 разрешено остановить ровно один
подтверждённый systemd unit, доказать отсутствие polling owners, создать новый
verified backup в `/opt/barbot-backups/production` и проверить backup-копию.
Каждый timestamp target должен быть новым и не перезаписываться. Требуемое
permission hardening выполняется только после verified backup, пока service
остановлен, и до migration/start. Переход к migration не автоматический.

### Stage 2 — migration/import

Только после отдельного подтверждения Stage 2 и verified backup разрешены
официальные project migration/import entrypoints. Ручной SQL downgrade и
импровизированное исправление production DB запрещены.

### Stage 3 — start/acceptance

Только после отдельного подтверждения Stage 3 разрешены запуск одного instance
и контролируемый acceptance. Нельзя выбирать test actor/recipient наугад.

### Stage 4 — production enablement

Только после отдельного подтверждения Stage 4 разрешено утверждённое production
enablement. Разрешения и recipients нельзя расширять автоматически.

## Mandatory stop conditions

Немедленно остановиться при HEAD mismatch, dirty tree, конфликте инструкций,
невозможности скрыть secrets, более чем одном polling owner, неизвестном
service owner, integrity/FK failure, backup/migration failure, неожиданной seed
mutation, неизвестном test recipient, риске неожиданной массовой рассылки или
необходимости менять application code во время rollout. Изменение кода требует
отдельной development/fix phase с тестами и новым подтверждённым HEAD.

## No self-escalation

Изменение этого файла в текущем turn не даёт права немедленно использовать
новые production-разрешения. Они начинают действовать только в следующем
отдельном задании после commit. Агент не может сам объявить обычную задачу
production rollout, а разрешение пользователя на один Stage не переносится на
следующий.

## Architecture and delivery rules

Не менять существующие стабильные ID каталога. Каталог хранится в JSON;
пользователи и изменяемые роли — в SQLite. `super_admin` задаётся окружением и
не изменяется через Telegram. Не добавлять ORM, Redis или PostgreSQL без
отдельного решения владельца. Handlers не выполняют SQL напрямую; storage не
содержит Telegram UI. Все permissions вычисляются централизованно.

Каждый этап оформляется отдельным коммитом только по явной команде владельца.
После изменений запускать pytest, compileall, import check и git diff --check.

## KOS project-memory protocol

BarBot uses the local knowledge entrypoint `memory/MEMORY_INDEX.md` and the
shared, project-neutral methodology under `global-kos/`.  The production-safety
rules above always take precedence over memory guidance.  Memory is never an
authorization for production access or an alternative to a required live
preflight.

Classify each task before broad source reads:

- **Lightweight**: direct questions, status checks, and small localized work.
  Read `memory/MEMORY_INDEX.md` and only the smallest relevant memory slice.
- **Standard**: normal implementation, multi-file documentation, feature work,
  or non-trivial debugging.  Also check current context and working memory; use
  a targeted source-localization pass before opening code broadly.
- **Full**: architecture, high-risk or unclear work, stale/conflicting memory,
  broad impact, or multi-turn work.  Apply Standard workflow plus trust,
  freshness, lifecycle, and compression review for the relevant slice.

For all modes:

1. Read `memory/MEMORY_INDEX.md` first and follow only the route relevant to the
   task; templates and archive are not default reading.
2. Revalidate stale, owner-confirmed, and runtime-sensitive facts before any
   consequential action.
3. Read code/tests when exact control flow, permissions, data shape, or current
   behavior matters; memory does not replace source evidence.
4. Never start production work from memory alone.  The staged rollout policy
   and explicit per-stage authorization remain mandatory.
5. Update existing memory after material architecture or workflow changes when
   it will make later work faster or safer.  Do not create notes without a
   practical routing or reuse benefit.
6. Standard/Full tasks participating in the active utility pilot record one
   honest, redacted local event as described in
   `memory/02-working/MEMORY_UTILITY_LOG.md`.  Unknown values stay `unknown`.
   No network or Telegram delivery is part of the BarBot pilot.
7. Use `rg`, import maps, and targeted tests as the localization fallback when
   Graphify or PowerShell is unavailable; do not install them for routine work.
8. Keep project facts in `memory/`; keep reusable templates and methodology in
   `global-kos/`.  Use repo-relative paths in all memory documents.

The pilot is reviewed after 10 eligible Standard/Full observations.  Track
usefulness and overhead honestly; refine or stop the pilot if it becomes
ceremonial or misleading.
