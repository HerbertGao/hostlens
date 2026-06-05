# Follow-ups (add-service-inspector-contract-spike)

Findings registered during the spike that are **explicitly out of this change's
scope** and must be handled by independent follow-up work. Recorded here so they
are tracked once this change is archived (the seeds are grandfathered legacy per
`specs/service-inspector-contract/spec.md` first requirement — not an internal
contradiction of the contract).

## FU-1 — Migrate pre-spike seed secret names to the `HOSTLENS_` prefix (D-6)

The new `service-inspector-contract` mandates secret env names use the
`HOSTLENS_` prefix so they survive SSH via remote sshd `AcceptEnv HOSTLENS_*`
(aligned with `ssh-execution-target` spec :120-122). Two **pre-spike** seed
inspectors drift from this — they were authored before the contract and use
**non-`HOSTLENS_`** names:

| Seed inspector | Current secret | Should migrate to |
|---|---|---|
| `redis.slowlog` (`src/hostlens/inspectors/builtin/redis/slowlog.yaml`) | `REDIS_PASSWORD` | `HOSTLENS_REDIS_PASSWORD` |
| `postgres.bloat_tables` (`src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml`) | `PGPASSWORD` | `HOSTLENS_PGPASSWORD` |

**Impact**: on a remote host whose sshd is configured with `AcceptEnv
HOSTLENS_*` (the documented secret-delivery path), these non-`HOSTLENS_` env
vars are silently dropped by the sshd `AcceptEnv` allowlist → the client never
receives the password → the inspector authenticates as no-password / fails. The
contract's new probes (`redis.memory_usage` / `mysql.connection_usage`) are
correct from the start (`HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`).

**Why not migrated here**: these are already-archived seed inspectors; migrating
them touches a different change's deliverables and would expand this 2-probe
contract spike's scope. The contract explicitly grandfathers them.

**Follow-up work** (separate proposal): for each seed, rename the declared
secret to the `HOSTLENS_` form and remap it inside the collector to the client's
native env channel (`redis.slowlog`: `REDISCLI_AUTH`; `postgres.bloat_tables`:
`PGPASSWORD`), re-record fixtures, and update any docs/operations references.
Additionally `redis.slowlog` currently passes the password as an argv flag
(`-a "$..."`, visible in a global `ps`) — the migration should also move it to
the `REDISCLI_AUTH` env channel so it stops leaking via argv.

## FU-3 — Keep recorder passwords out of the host `argv`

The one-shot recording-lane helpers (`tests/inspectors/_record_*.py`) still
pass throwaway passwords on the **host** `docker exec` command line — MySQL via
`-p<pw>` (`_record_mysql_connection_usage.py`) and Redis via `-a <pw>` /
`CONFIG SET requirepass <pw>` (`_record_redis_memory_usage.py`). The password is
therefore visible in the *host's* process list while a fixture is being
re-recorded. This does not affect the shipped inspector contract (the collectors
themselves use `MYSQL_PWD` / `REDISCLI_AUTH` env and never put the secret in
`argv`) nor the recorded fixtures (already redacted), and the values are
throwaway credentials for ephemeral local containers.

**Follow-up work**: route the recorder auth through `docker exec -e VAR`
(inheriting the value from the docker client's environment, never `-e VAR=value`
which would re-leak into `argv`) so the recording lane matches the same
"password never in argv" discipline the inspectors enforce. Recorder-only
tooling, so deferred out of this 2-probe spike.

## FU-2 (optional) — Inspector-level SSH auth-success verification

The contract treats local/SSH behavioral equivalence as **structural** (CI
verifies on `local`; no per-inspector real-SSH container test, same as wave-1).
An optional follow-up could add an inspector-level SSH auth-success test, but it
**must** use a real sshd container configured with `AcceptEnv HOSTLENS_*` (NOT
the ssh-target self-test's `HOSTLENS_TEST_*`, which would not pass through
`HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`). Listed as optional; not
required for this change.
