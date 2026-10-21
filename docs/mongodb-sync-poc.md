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
A failed run leaves `poc_sync_state.status=failed`; retrying is idempotent and
the successful checkpoint is not advanced on failure. During a run, readers
may observe partial POC data. Do not point telemetry at these collections.

### Required configuration

In GitHub repo **Settings -> Secrets and variables -> Actions**:

| Type | Name | Notes |
| --- | --- | --- |
| Secret | `PRODUCTION_DATABASE_URL` | Read-only credential for the source PostgreSQL DB, SSL/TLS enabled |
| Secret | `MONGODB_URI` | Atlas DB user with minimum write permissions on the **POC** database |
| Variable (optional) | `MONGODB_DATABASE` | Defaults to `ouros_analytics_poc`; must start with that prefix |
| Variable | `MONGO_SYNC_POC_ENABLED` | Set to `true` to opt in to **branch-push** sync testing |
| Variable | `MONGO_SYNC_ENABLED` | Set to `true` to opt in to manual/scheduled sync **after merge** |

Do not put credentials in YAML, URLs, PR comments or logs. Atlas Network
Access must allow the runner's outbound address; GitHub-hosted runners have
changing egress IPs. Use a controlled egress strategy if a narrow allowlist is
required, and scope the MongoDB user strictly to the POC database.

### Running the experiment

1. Create the Atlas POC database user and configure the two repository secrets.
2. Run unit tests on the branch:
   `python -m pip install -r requirements.txt && python -m unittest discover -s tests -p 'test_sync_mongodb.py' -v`.
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
