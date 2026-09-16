# BarBot production rollback runbook

Rollback after a migration restores the consistent pre-deploy backup. There is
no manual schema downgrade and no attempt to combine a v11 database with code
that expects an older schema.

This runbook is usable only inside an explicitly authorized Phase 10B Stage.
Rollback authority does not reveal secrets, authorize an unconfirmed service
unit, or carry permission into a later Stage. Secret values must never appear
in diagnostics or reports.

## Immediate stop conditions

- more than one polling owner;
- migration loop, schema validation failure, integrity/FK failure;
- seed conflict or unexpected counts;
- wrong server/repository/path/recipient;
- repeated or unintended Telegram sends;
- report corruption or correction/schedule isolation failure;
- permission regression or secret exposure.

## Procedure

1. Stop the operator-confirmed service and prove polling/workers stopped.
2. Record UTC time, current Git HEAD, failing schema version, relevant safe log
   timestamps, and user-visible symptoms. Do not collect environment dumps,
   tokens, message bodies, or unrelated personal data.
3. Preserve the failed database with the SQLite backup API into a new,
   restrictive incident directory. Never overwrite the pre-deploy backup.
4. Verify the pre-deploy `SHA256SUMS`, `sqlite-check.txt`, file size/mode, and
   recorded Git HEAD. Validate `PRAGMA integrity_check` and
   `foreign_key_check` on a disposable copy of the backup.
5. Move the failed production DB and its current `-wal`/`-shm` aside as one
   timestamped incident set while the service is stopped. Do not delete them.
6. Restore `barbot.db` from the consistent pre-deploy backup to a new temporary
   file on the same filesystem, set owner/mode, fsync it, and atomically rename
   it to the configured DB path. Do not restore stale WAL/SHM beside it.
7. Reopen the restored DB offline and confirm checksum expectation,
   `integrity_check=ok`, zero FK violations, and the recorded pre-deploy schema.
8. Restore the previous approved Git revision. Confirm clean tree and that its
   supported schema matches the restored DB before startup.
9. Restore report and audit artifacts only when the incident changed them and
   only from their separately checksummed backups. Never mix an old report
   directory with DB metadata from a different backup point.
10. Start exactly one service instance. Check startup logs and perform a
    read-only `/start`, permissions, report-history, and scheduler-health smoke
    test before any new warehouse mutation or delivery.
11. Record the outcome and retain both the pre-deploy backup and failed-state
    incident set according to the owner's retention policy.

Telegram messages already delivered cannot be rolled back. Do not delete or
silently resend them. Record affected report version, recipient, timestamp,
and delivery record in the incident; communicate correction through the owner-
approved operational process.

If the restored schema and previous code cannot be proven compatible, keep the
service stopped and declare the rollout blocked. Never improvise SQL downgrade
statements in production.
