# Running the API test suite on a shared farm

The farm's Postgres (`plane-db`) is shared between agents. Two rules keep one
run from corrupting another and keep an environment failure from being read as a
code failure.

## 1. Every run gets its own test database, created and dropped by the run (BIP-63)

`plane/tests/conftest.py` names the test database from xdist's own identifiers:
`test_<db>[_<agent>]_<worker_id>_<testrun_uid>`. `testrun_uid` is shared by all
workers of one run (so the database is invocation-owned — no other run can reach
it) and `worker_id` (`gw0`.. under `-n`, else `master`) gives each parallel
worker its own database. No two runs collide — not two agents, not the same
agent twice, not two containers sharing a PID, not two xdist workers. Because the
name is unique, `--reuse-db` cannot mean reuse — `pytest_configure` turns it off
so pytest-django's own database fixture **creates and then drops** the database,
owning connection handling and teardown order. Nothing leaks in the normal case,
and there is no global drop-loop that could reach another agent's live database.

You do not have to do anything. Each worker PRINTS its own exact database name
(every worker, so under `-n` you see all of them):

```
BIP-63 isolated test database [gw0]: test_plane_<agent>_gw0_<testrun_uid>
BIP-63 isolated test database [gw1]: test_plane_<agent>_gw1_<testrun_uid>
```

Optionally set `BIP_TEST_DB_SUFFIX` to your agent name so that name carries a
readable prefix.

**Trade, stated plainly:** every run builds a fresh schema (fast under the
default `--nomigrations`) — there is no reuse speed-up. Isolation that actually
holds is worth the seconds; a leaked database is a slow failure, a collision an
instant one that lies to whoever it hits.

**Stale residue after a hard kill (SIGKILL, OOM, disk-full).** Owned teardown
does not run if the process is killed, so a database can survive. Drop it by the
**EXACT name** from that run's header — nothing else:

```sql
DROP DATABASE IF EXISTS "test_plane_<agent>_gw0_<testrun_uid>";  -- the exact printed name
```

Do NOT sweep by prefix, and do NOT drop "databases with no active connections":
between a run's `CREATE DATABASE` and its first connection there is a window in
which a live run's database has no connections, so such a sweep can drop a run
that is about to use it. Only the exact name you mean to.

## 2. A run whose control does not reproduce the baseline is measuring the environment

Before reporting a result as a property of the *change* (a mutant kill, a
regression, a pass), run the control — the unmodified base, the same node ids. If
the control does not reproduce the known baseline, you are measuring the
environment (a corrupted shared DB, a full disk, a missing broker), not the code,
and base and head are no longer comparable. This caught both the shared-DB
corruption and the disk-full incident on 2026-08-14; it is the standing
instrument, not a one-off remedy.
