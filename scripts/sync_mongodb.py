#!/usr/bin/env python3
"""Isolated MongoDB Atlas analytics sync proof of concept.

Full reconciliation is deliberate for the POC: retries, source deletes and old
corrections are handled without depending on complete updated_at coverage.
The existing PostgreSQL sync remains unchanged.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import chain
from typing import Iterable

import psycopg2
from bson import BSON, Decimal128
from pymongo import MongoClient, ReplaceOne

from scripts.sync_analytics import (
    STEPS,
    TIP_FEEDBACK_COLUMNS,
    TIP_FEEDBACK_CONFLICT,
    TIP_FEEDBACK_SNAPSHOT_QUERY,
    WATER_REGISTRY_QUERY,
)

BATCH_SIZE = 500
BUCKET_READINGS = 250
MAX_BSON_BYTES = 12 * 1024 * 1024  # headroom under MongoDB's 16 MiB limit
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SYNC_ID = "production_to_mongodb_poc"
COLLECTION_ROWS = "poc_analytics_rows"
COLLECTION_MONTHLY = "poc_farm_monthly"
COLLECTION_BUCKETS = "poc_metric_buckets"
COLLECTIONS = (COLLECTION_ROWS, COLLECTION_MONTHLY, COLLECTION_BUCKETS)

ENERGY_REGISTRY_QUERY = """
    SELECT e.id, f.id, f.id_enterprise, e.registration_date::date,
           e.energy_consumption::numeric
    FROM public.energy_registries e
    JOIN public.farms f ON f.id = e.id_farm
    ORDER BY 2, 4, 1
"""


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def bson_value(value):
    """Retain decimal precision and normalize PostgreSQL date values."""
    if isinstance(value, Decimal):
        return Decimal128(str(value))
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (list, tuple)):
        return [bson_value(item) for item in value]
    return value


def date_value(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def document_for(dataset: str, columns: tuple[str, ...],
                 keys: tuple[str, ...], row: tuple, run_id: str) -> dict:
    data = {name: bson_value(value) for name, value in zip(columns, row, strict=True)}
    identifier = ":".join(str(row[columns.index(key)]) for key in keys)
    data["_id"] = f"{dataset}:{identifier}"
    data["dataset"] = dataset
    data["_sync_run"] = run_id
    if dataset == "fact_farm_consumption_monthly":
        data["_id"] = f"{data['farm_id']}:{date_value(row[0]).isoformat()}"
        data["water_metrics"] = {"monthly_total_m3": data.pop("water_consumed_m3")}
        data["energy_metrics"] = {"monthly_total_kwh": data.pop("energy_consumed_kwh")}
    return data


def measurement_buckets(metric: str, rows: Iterable[tuple], run_id: str,
                        max_readings: int = BUCKET_READINGS):
    """Chunk sorted records per farm/metric/month; never grow unbounded arrays.

    The SQL source must sort by farm, registration date, then source ID.
    """
    if max_readings < 1:
        raise ValueError("max_readings must be positive")
    current_key = None
    current_rows = []
    part = 0
    enterprise_id = None

    def emit(key, entries, chunk, enterprise):
        kind, farm_id, month = key
        document = {
            "_id": f"{kind}:{farm_id}:{month}:{chunk:04d}",
            "metric": kind,
            "farm_id": farm_id,
            "enterprise_id": enterprise,
            "month": month,
            "part": chunk,
            "readings": entries,
            "_sync_run": run_id,
        }
        if len(BSON.encode(document)) > MAX_BSON_BYTES:
            raise ValueError("MongoDB metric bucket exceeded 12 MiB safety limit")
        return document

    for row in rows:
        # Water rows: (id, farm, enterprise, name, region, date, m3).
        # Energy rows: (id, farm, enterprise, date, kWh).
        recorded = date_value(row[5] if metric == "water" else row[3])
        amount = row[6] if metric == "water" else row[4]
        key = (metric, int(row[1]), recorded.strftime("%Y-%m"))
        if current_key is not None and (key != current_key or len(current_rows) >= max_readings):
            yield emit(current_key, current_rows, part, enterprise_id)
            part = part + 1 if key == current_key else 0
            current_rows = []
        current_key = key
        enterprise_id = int(row[2])
        current_rows.append({
            "source_id": int(row[0]),
            "date": bson_value(recorded),
            "value": bson_value(amount),
        })
    if current_key is not None:
        yield emit(current_key, current_rows, part, enterprise_id)


def mongo_compatible_query(dataset: str, query: str) -> str:
    """Adapt legacy analytics extracts to the current operational schema.

    These changes affect only the Mongo POC, not the live PostgreSQL sync.
    """
    if dataset == "dim_farm":
        # Removed by remover_state_goal_farm_id_and_farms_place.sql and
        # remover_poultry_capacity_farms.sql. Preserve document fields as null.
        query = query.replace("btrim(f.place)", "NULL::text")
        query = query.replace("f.poultry_capacity", "NULL::integer")
    elif dataset == "fact_goal":
        # State goals now belong to farms through farm_goals and the status
        # is stored on each association rather than on state_goals.
        replacements = {
            "SELECT g.id, 'state', g.id_farm,":
                "SELECT g.id, 'state', fg.id_farm,",
            "lower(btrim(g.status)), btrim(g.title), g.target_value,\n               g.date_creation":
                "lower(btrim(fg.status)), btrim(g.title), g.target_value,\n               g.date_creation",
            "FROM public.state_goals g\n        JOIN public.farms f ON f.id = g.id_farm":
                "FROM public.state_goals g\n        JOIN public.farm_goals fg ON fg.id_goal = g.id\n        JOIN public.farms f ON f.id = fg.id_farm",
        }
        for old, new in replacements.items():
            if old not in query:
                raise RuntimeError(f"Legacy {dataset} query changed: {old!r}")
            query = query.replace(old, new)
    return query

def stream_rows(source, query: str, params=None, *, name: str):
    """Bounded reads inside the source REPEATABLE READ snapshot."""
    with source.cursor(name=name) as cursor:
        cursor.itersize = BATCH_SIZE
        cursor.execute(query, params)
        while batch := cursor.fetchmany(BATCH_SIZE):
            yield from batch


def write_docs(collection, docs: Iterable[dict]) -> int:
    batch = []
    count = 0
    for document in docs:
        batch.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
        if len(batch) >= BATCH_SIZE:
            collection.bulk_write(batch, ordered=True)
            count += len(batch)
            batch = []
    if batch:
        collection.bulk_write(batch, ordered=True)
        count += len(batch)
    return count


def create_indexes(db):
    db[COLLECTION_ROWS].create_index([("dataset", 1), ("farm_id", 1)])
    db[COLLECTION_ROWS].create_index([("dataset", 1), ("enterprise_id", 1)])
    db[COLLECTION_MONTHLY].create_index([("farm_id", 1), ("month_start", 1)])
    db[COLLECTION_MONTHLY].create_index([("enterprise_id", 1), ("month_start", 1)])
    db[COLLECTION_BUCKETS].create_index([("farm_id", 1), ("metric", 1), ("month", 1)])
    db[COLLECTION_BUCKETS].create_index([("enterprise_id", 1), ("metric", 1), ("month", 1)])


def sync_snapshot(source, db, run_id: str, sync_end: datetime) -> dict[str, int]:
    """Write complete snapshots; prune source deletions only after all writes."""
    counts = {}
    create_indexes(db)

    for dataset, query, columns, conflict in STEPS:
        collection = COLLECTION_MONTHLY if dataset == "fact_farm_consumption_monthly" else COLLECTION_ROWS
        query = mongo_compatible_query(dataset, query)
        rows = stream_rows(source, query, (EPOCH, sync_end), name=f"mongo_{dataset}")
        docs = (document_for(dataset, columns, conflict, row, run_id) for row in rows)
        counts[dataset] = write_docs(db[collection], docs)

    feedback = stream_rows(source, TIP_FEEDBACK_SNAPSHOT_QUERY, name="mongo_feedback")
    docs = (document_for("fact_tip_feedback", TIP_FEEDBACK_COLUMNS,
                         TIP_FEEDBACK_CONFLICT, row, run_id) for row in feedback)
    counts["fact_tip_feedback"] = write_docs(db[COLLECTION_ROWS], docs)

    water_query = WATER_REGISTRY_QUERY + " ORDER BY 2, 6, 1"
    for metric, query in (("water", water_query), ("energy", ENERGY_REGISTRY_QUERY)):
        rows = stream_rows(source, query, name=f"mongo_{metric}_readings")
        buckets = measurement_buckets(metric, rows, run_id)
        counts[f"{metric}_buckets"] = write_docs(db[COLLECTION_BUCKETS], buckets)

    if counts.get("dim_farm", 0) == 0:
        raise RuntimeError("POC safety: refusing to prune on empty farm snapshot")

    # A full source snapshot also catches hard deletes and link removals.
    # Do not prune before all datasets have been written successfully.
    for name in COLLECTIONS:
        db[name].delete_many({"_sync_run": {"$ne": run_id}})
    return counts


def main() -> None:
    uri = required_env("MONGODB_URI")
    db_name = os.getenv("MONGODB_DATABASE", "ouros_analytics_poc")
    if not db_name.startswith("ouros_analytics_poc"):
        raise RuntimeError("POC safety: MONGODB_DATABASE must begin with ouros_analytics_poc")
    run_id = uuid.uuid4().hex
    client = MongoClient(uri, serverSelectionTimeoutMS=10000, retryWrites=True)
    source = None
    state = None
    try:
        client.admin.command("ping")
        db = client[db_name]
        state = db["poc_sync_state"]
        source = psycopg2.connect(required_env("PRODUCTION_DATABASE_URL"))
        source.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        with source.cursor() as cursor:
            cursor.execute("SELECT CURRENT_TIMESTAMP")
            sync_end = cursor.fetchone()[0]
        state.update_one(
            {"_id": SYNC_ID},
            {"$set": {"status": "running", "last_attempt_at": datetime.now(UTC),
                      "run_id": run_id}},
            upsert=True,
        )
        counts = sync_snapshot(source, db, run_id, sync_end)
        source.commit()
        state.update_one(
            {"_id": SYNC_ID},
            {"$set": {"status": "success", "last_sync": sync_end,
                      "last_success_at": datetime.now(UTC), "run_id": run_id,
                      "counts": counts}},
        )
        print(f"MongoDB POC sync completed: {counts}")
    except Exception:
        if source is not None:
            source.rollback()
        if state is not None:
            state.update_one(
                {"_id": SYNC_ID},
                {"$set": {"status": "failed", "last_failure_at": datetime.now(UTC),
                          "last_error_type": "sync_failed"}},
                upsert=True,
            )
        raise
    finally:
        if source is not None:
            source.close()
        client.close()


if __name__ == "__main__":
    main()
