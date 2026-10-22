# MongoDB Atlas synchronization POC

This experiment lives in the **existing** `ouros-analytics-database` repository.
It does not change `scripts/sync_analytics.py`, the homelab deployment, the
PostgreSQL schema or the telemetry service.

## Data flow

`Neon/PostgreSQL production (read-only)` -> `scripts/sync_mongodb.py` ->
`MongoDB Atlas, ouros_analytics_poc*`

- `poc_analytics_rows`: stable-ID records for dimensions, lots, payments,
  goals and tip feedback. The record type is identified by `dataset`.
- `poc_farm_monthly`: one monthly aggregate per farm, including
  `water_metrics.monthly_total_m3` and
  `energy_metrics.monthly_total_kwh`.
- `poc_metric_buckets`: water/energy registry histories, partitioned by
  farm, metric and month. Documents are further split into at most 250
  readings, with a 12 MiB encoded-size guard, below MongoDB's 16 MiB limit.
- `poc_sync_state`: status, last successful source snapshot timestamp,
  counts, and run identifier.

The script reuses the SQL extraction contracts from the existing synchronizer.
For the POC it **fully reconciles** each source dataset per run: it overwrites
records with deterministic keys, then removes records absent from the source
only after all datasets have been written. This covers corrections and hard
deletes without relying on `updated_at` for deletion detection, but is not
an efficient incremental sync for a large production dataset.

The PostgreSQL source transaction uses REPEATABLE READ and read-only access.
The MongoDB bulk writes are **not one atomic cross-collection transaction**.
Failures after `poc_sync_state` is initialized set `status=failed`;
configuration and MongoDB connection errors may occur before that state is
available. Retrying is idempotent, and the successful checkpoint is not
advanced on failure. During a run, readers may observe partial POC data.
Do not point telemetry at these collections.

### Required configuration

In GitHub repo **Settings -> Secrets and variables -> Actions**:

| Type | Name | Notes |
| --- | --- | --- |
| Secret | `PRODUCTION_DATABASE_URL` | Read-only source credential; `sslmode=verify-full` required when set in URI (or omitted to use enforced default); hostname and certificate checked with trusted system CA, or a configured trusted CA |
| Secret | `MONGODB_URI` | Atlas DB user restricted to the **POC** database; TLS and certificate/hostname checks enforced, insecure URI options rejected |
| Variable (optional) | `MONGODB_DATABASE` | Defaults to `ouros_analytics_poc`; must start with that prefix |
| Variable | `MONGO_SYNC_POC_ENABLED` | Set to `true` to opt in to **branch-push** sync testing |
| Variable | `MONGO_SYNC_ENABLED` | Set to `true` to opt in to manual/scheduled sync **after merge** |

The PostgreSQL helper requires `sslmode=verify-full` for source-server
authentication, including DNS hostname validation. If the URI omits
`sslmode`, the helper selects `verify-full`. If a custom CA file is not
specified through `sslrootcert` or `PGSSLROOTCERT`, it uses the
trusted Ubuntu/Debian OS CA bundle (`/etc/ssl/certs/ca-certificates.crt`) when
available, then `/etc/ssl/cert.pem`, falling back to `sslrootcert=system`.
An explicit CA path avoids incompatible trust-store discovery with bundled
libpq/OpenSSL in `psycopg2-binary`. All modes still verify certificate
chains and hostname identity. Explicit `sslmode=require` or `verify-ca` is
rejected, so an existing `?sslmode=require` repository secret needs to be
updated to `?sslmode=verify-full`; **never weaken verification to make a
connection succeed**. The host must match the Neon TLS certificate.

MongoDB connections use TLS with normal certificate/hostname verification.
URI flags enabling `tlsInsecure`, `tlsAllowInvalidCertificates`, or
`tlsAllowInvalidHostnames`, and URI flags disabling TLS, cause the job to
fail before any MongoDB network connection.

Do not put credentials in YAML, URLs, PR comments or logs. Atlas Network
Access must allow the runner's outbound address; GitHub-hosted runners have
changing egress IPs. Use a controlled egress strategy if a narrow allowlist is
required, and scope the MongoDB user strictly to the POC database.

### Running the experiment

1. Create the Atlas POC database user and configure the two repository secrets.
2. Run unit tests on the branch:
   `python -m pip install -r requirements.txt && python -m unittest discover -s tests -p 'test_*mongodb*.py' -v`.
3. For a local one-off run, set `PRODUCTION_DATABASE_URL`, `MONGODB_URI`
   and (optionally) `MONGODB_DATABASE`, then run
   `python -m scripts.sync_mongodb`.
4. For a GitHub Actions branch test, opt in with
   `MONGO_SYNC_POC_ENABLED=true` and push a commit touching the POC
   workflow, sync script, tests or requirements to
   `experiment/mongodb-atlas-sync`. A branch push runs unit tests first,
   and only runs the live job when explicitly enabled.
5. Once the workflow exists on the default branch, its manual trigger and
   scheduled cron (`17,47 * * * *`, UTC) become available. They are
   disabled for live sync unless `MONGO_SYNC_ENABLED=true`.

GitHub `schedule` only runs on the default branch; a newly introduced
`workflow_dispatch` workflow cannot be manually dispatched until it exists
on the default branch.

### Automated verification

A branch push now performs **two consecutive syncs**, records business-content
SHA-256 fingerprints after the first one, then compares them after the second.
The audit CLI accepts only the action `snapshot` or `verify`, not a
user-provided filesystem path. Both actions use a private, fixed baseline
file (`/tmp/atlas-poc-baseline.json`) on the isolated Ubuntu GitHub runner,
created with permissions `0600` and exclusive creation. Symlink following
is disabled for reading and writing; the snapshot is not overwritten.
This removes untrusted CLI file paths and avoids path traversal.

The audit checks every SQL-derived document and both water/energy registry
buckets against Atlas. It also independently recalculates farm/month water and
energy totals from source registries, explains the zero-payment case by
counting source `payments`, and reports per-collection BSON bytes.

Where supported, the audit also queries `dbStats` for data, storage and index
bytes. Shared Atlas tiers or limited database roles may reject that command;
in that case the fallback is raw BSON size only, **not** a definitive Atlas
storage-quota or billing measurement. Review Atlas Cluster Metrics to confirm
storage headroom.

The double-sync verification runs only on the experimental branch's push
workflow. If the workflow is later merged and scheduled, it performs a single
regular sync instead. A source record updated during the two snapshots can
produce a strict fingerprint mismatch; rerun the audit rather than suppressing
the check.

### Acceptance checks before migrating telemetry

- Initial sync produces nonempty documents where production has source data.
- Repeating the same snapshot does not duplicate records.
- Correcting an old water or energy reading updates the corresponding bucket.
- Deleting a source reading removes it on the next **successful** run.
- Failure does not advance `last_sync`; a retry returns to success.
- Sample monthly totals match existing PostgreSQL Analytics views.
- Index and collection sizes fit the Atlas tier, and runtime meets expected
  GitHub Actions frequency.

**Out of scope:** incremental/CDC optimization, production-grade snapshot
publication, telemetry provider migration, and disabling homelab. These are
follow-up decisions after source-to-Atlas correctness is demonstrated.
