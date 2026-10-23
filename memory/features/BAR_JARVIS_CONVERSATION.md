# Bar Jarvis Conversation Brain

Scope: bounded multi-turn context, confirmed memory and guarded tool loop
Trust: implemented and tested offline; live provider/shadow evidence pending
Lifecycle: Development
Last verified: 2026-10-23

`message → ContextSnapshot → Stage 1 route → local validation → optional Stage
2 extraction → server capability → result/preview → atomic context update`.

The provider never owns permissions, stable IDs, risk, revisions,
confirmation, execution, SQL, shell, filesystem, systemd or secrets. A snapshot
contains at most 12 turns/12,000 recent characters, a 2,000-character rolling
summary, five candidates, eight confirmed memories, 500 tool-result characters
and 24,000 serialized bytes. Actor/chat numeric identifiers, raw DB rows, audit
data, full catalog and capabilities outside the runtime surface are excluded.

Eviction is deterministic oldest-first. Summary uses only server-confirmed
topic/entity/task/capability state; provider prose never becomes long-term
memory. Summary/session updates share the v13 optimistic revision. Failed
lookup/provider calls do not clear the last successful stable entity. Reset
deletes only the AI conversation session, not warehouse FSM/drafts.

Schema v15 adds `assistant_long_term_memories`. DDL has no transaction control;
the migration coordinator owns atomic v14→v15 creation, validation and
`user_version`. Records are owner-isolated, opaque-tokened, revisioned, scoped,
typed and optionally expiring. Even explicit “запомни …” creates a persistent,
expiring preview; only its owner can confirm it after access recheck. Recall is
read-only. Deletion has a separate preview/confirmation and ambiguous deletion
requires exact selection. Duplicate confirmation is idempotent.

References resolve only to server-owned stable IDs. Pronouns do not require
grammatical gender; first/second/last use bounded candidates; “туда же” requires
a prior confirmed destination. Ambiguity produces one clarification, never a
fuzzy mutation selection.

At most two runtime-allowlisted tool steps are permitted. Reads may run
immediately. A mutation produces an opaque preview/proposal then stops; existing
confirmation, permission/revision recheck, idempotent execution and audit remain
authoritative.

Corpus `bar-jarvis-conversation-v1` contains 60 dialogues and 180 user turns,
including every 41 registered capabilities. The frozen runner labels its network-
free deterministic result `offline_scripted`, reports expected/actual contracts
and safety side-effect counts, and writes bounded JSON only to an explicit path.
This validates the runner and application contracts, not provider accuracy.
Live/shadow route, entity and reference quality remains `pending — requires
controlled shadow evaluation`. Voice, web, images and video also remain pending.
