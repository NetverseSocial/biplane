# BIP-42 Apply Failure States

`apply-update.sh` is a host command. It fetches JSON metadata and pulls container
images by registry digest; it downloads and extracts no archive.

| Last completed step | Reported state | Pins and services | Database | Operator action |
|---|---|---|---|---|
| Release grammar / ordering | `refused: selection` | Unchanged | Unchanged | Use one canonical stable tag that is strictly newer than the running release. Invalid tags stop before transport; equal and older tags stop after exact metadata and baseline readback but before backup or pull. |
| Exact-tag metadata | `refused: metadata` | Unchanged | Unchanged | Correct the tag, source, transport allowlist, or release metadata. No backup or pull occurred. |
| Running baseline readback | `refused: baseline` | Unchanged | Unchanged | Reconcile persisted/rendered pins with all six active image references, the API release/build identity, and one bundled database target shared by API, migrator, and the running `plane-db` process used for backup. External targets and plane-db-only user/database overrides stop before backup or pull. A stale pin or same-version image drift never authorizes an update. |
| Level admission | `refused: full` | Unchanged | Unchanged | Use the documented manual image/runtime upgrade path. Apply never treats `full` as `code` or `data`. |
| Backup | `failed: backup` | Unchanged | Unchanged | Fix the backup target or bundled database. No image was pulled and no service stopped. |
| Digest pulls / image identity | `failed: image` | Unchanged; downloaded images are inert | Unchanged | Correct the registry/release. Backend release/build and both displayed frontend build ids must agree. The old deployment continues running. |
| Migration plan | `refused: level` or `failed: plan` | Unchanged | Unchanged | A `code` release with pending migrations is refused. Correct the producer level or the release image. |
| Data migration | `recovery_required: migration` | Old `.env` remains; mutation-serving services stay stopped | May be partially migrated | Inspect the named dump and migration output. Restore or reconcile explicitly, then restart the old deployment. Never retry blind. |
| Atomic `.env` commit | `failed: pin_commit` | Rename yields old or new complete bytes, never a partial file; the command restores the saved config and reports if that restoration also fails | For `data`, migration may already be applied | A directory-sync failure after rename is reported as an uncertain commit. For `data`, reconcile the database before restarting services even when old pins were restored. |
| Recreate / digest / health / served-build readback (`code`) | `failed: activation`, old release restored or `recovery_required` | Failed services stopped; saved `.env` checksum-verified and restored atomically; old services are claimed restored only when the complete captured snapshot and health match | Unchanged | Inspect the failed new services. If old snapshot readback failed, repair explicitly; the command does not call that restoration. |
| Recreate / digest / health / served-build readback (`data`) | `recovery_required: migration_applied_activation_failed` | Failed/partial new services stopped; saved `.env` checksum-verified and restored; old services are not claimed healthy | New schema may be present | Use the named dump and migration log to choose database restore or forward repair before starting either release. |
| All readbacks | `succeeded` | Exact four digest pins persisted; all six Biplane services run the selected references; API and served web/admin identities match | Migration complete when level is `data` | Retain the backup per local policy. |

## Backup contents

Each attempted apply that reaches backup creates a private directory containing:

- `config.env`: byte-for-byte pre-update deployment configuration;
- `compose.rendered.yaml`: the pre-update rendered Compose configuration;
- `deployment-snapshot.json`: active image references and local IDs, API
  release/build identity, and the password-free normalized database target;
- `database.dump`: PostgreSQL custom-format dump;
- `release.json`: the complete selected executable identity;
- `SHA256SUMS`: checksums for every artifact above, including the selected
  `release.json` metadata.

`pg_restore --list` must read the dump before any pull or migration starts. The
command verifies `SHA256SUMS` before using `config.env` for rollback. This
is an integrity/readability check, not a claim that an unattended restore was
rehearsed. The operator owns database restoration, and the command says so at
the failure point where that distinction matters.
