# Permission Model

Scope: effective role capabilities
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: `app/models/user.py`, `app/services/access_service.py`, `tests/test_access_service.py`
Depends on: environment super-admin set and SQLite user roles
Importance: Critical

`super_admin` is resolved from environment configuration and is not a storable
SQLite role. All other roles are stored in SQLite. Visible buttons are only UX;
services/storage repeat permission checks before mutations.

| Capability | super_admin | admin | bartender | accountant | user | blocked |
|---|---:|---:|---:|---:|---:|---:|
| View catalog | yes | yes | yes | no | yes | no |
| Manage catalog/users/nomenclature | yes | yes | no | no | no | no |
| View warehouse | yes | yes | yes | yes | no | no |
| Mutate warehouse | yes | yes | yes | no | no | no |
| Use personal attendance | yes | yes | yes | no | no | no |
| Manage attendance | yes | yes | no | no | no | no |
| Manage rules/recipients/reminders/schedules | yes | yes | no | no | no | no |
| Manage closed-shift corrections | yes | yes | no | no | no | no |
| View corrections/delivery history | yes | yes | no | yes | no | no |
| Send warehouse reports | yes | yes | yes | no | no | no |
| Assign `admin` | yes | no | no | no | no | no |
| Assign bartender/accountant/user/blocked | yes | yes, except admin targets/self | no | no | no | no |

No actor can change their own role or a `super_admin`. An `admin` cannot create,
modify, or demote another `admin`. Exact permission-sensitive changes require a
fresh source/test check.
