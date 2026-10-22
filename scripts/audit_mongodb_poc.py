#!/usr/bin/env python3
"""Read-only verification of the isolated Atlas proof of concept.

The 'snapshot' phase records content fingerprints before a second sync.
The 'verify' phase checks repeatability, full source parity, domain totals,
and reports Atlas storage. No source or destination business data is logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from bson import BSON, Decimal128, json_util
from pymongo.errors import OperationFailure

from scripts.sync_analytics import (
    STEPS, TIP_FEEDBACK_COLUMNS, TIP_FEEDBACK_CONFLICT, TIP_FEEDBACK_SNAPSHOT_QUERY,
    WATER_REGISTRY_QUERY,
)
from scripts.sync_mongodb import (
    COLLECTION_BUCKETS, COLLECTION_MONTHLY, COLLECTION_ROWS, COLLECTIONS,
    ENERGY_REGISTRY_QUERY, EPOCH, SYNC_ID, date_value, document_for,
    connect_production_postgres, measurement_buckets, mongo_compatible_query,
    mongodb_client, stream_rows,
)

COLLECTION_STATE = "poc_sync_state"
REPORT_COLLECTIONS = (*COLLECTIONS,)
TOLERANCE = Decimal("0.000001")


def without_run(document: dict) -> dict:
    result = dict(document)
    result.pop("_sync_run", None)
    return result


def fingerprint(db) -> dict:
    """Cryptographic fingerprints exclude only the intentionally changing run ID."""
    summary = {}
    for name in REPORT_COLLECTIONS:
        digest = hashlib.sha256()
        count = 0
        for row in db[name].find({}).sort("_id", 1):
            stable = without_run(row)
            payload = json_util.dumps(stable, sort_keys=True).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
        summary[name] = {"count": count, "sha256": digest.hexdigest()}
    return summary


def compare_documents(db, collection_name: str, filter_by: dict,
                      source_documents, label: str) -> int:
    """Compare IDs and every business field, catching source-to-document key collisions."""
    expected = {}
    count = 0
    for original in source_documents:
        # Normalize SQL dates through BSON (millisecond precision, naive UTC).
        row = without_run(BSON(BSON.encode(original)).decode())
        identifier = row["_id"]
        if identifier in expected:
            raise AssertionError(f"{label}: duplicate source document identifier")
        expected[identifier] = row
        count += 1

    actual = {row["_id"]: without_run(row) for row in db[collection_name].find(filter_by)}
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise AssertionError(
            f"{label}: expected {len(expected)} source IDs, got {len(actual)} Atlas IDs"
        )
    for identifier, original in expected.items():
        if original != actual[identifier]:
            raise AssertionError(f"{label}: document content differs from source")
    print(f"PARITY OK {label}: {count} documents", flush=True)
    return count


def check_source_parity(source, db, run_id: str, last_sync: datetime) -> dict:
    """Verify every materialized document against SQL using the saved watermark."""
    results = {}
    for dataset, query, columns, keys in STEPS:
        query = mongo_compatible_query(dataset, query)
        rows = stream_rows(source, query, (EPOCH, last_sync), name=f"audit_{dataset}")
        generated = (
            document_for(dataset, columns, keys, row, run_id) for row in rows
        )
        coll = COLLECTION_MONTHLY if dataset == "fact_farm_consumption_monthly" else COLLECTION_ROWS
        results[dataset] = compare_documents(
            db, coll, {"dataset": dataset}, generated, dataset
        )

    feedback = stream_rows(source, TIP_FEEDBACK_SNAPSHOT_QUERY, name="audit_feedback")
    generated = (
        document_for("fact_tip_feedback", TIP_FEEDBACK_COLUMNS, TIP_FEEDBACK_CONFLICT, row, run_id)
        for row in feedback
    )
    results["fact_tip_feedback"] = compare_documents(
        db, COLLECTION_ROWS, {"dataset": "fact_tip_feedback"}, generated, "fact_tip_feedback"
    )

    for kind, query in (
        ("water", WATER_REGISTRY_QUERY + " ORDER BY 2, 6, 1"),
        ("energy", ENERGY_REGISTRY_QUERY),
    ):
        rows = stream_rows(source, query, name=f"audit_{kind}_buckets")
        generated = measurement_buckets(kind, rows, run_id)
        results[f"{kind}_buckets"] = compare_documents(
            db, COLLECTION_BUCKETS, {"metric": kind}, generated, f"{kind}_buckets"
        )
    return results


def numeric(value) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def check_monthly_totals(source, db) -> None:
    """Independent SQL aggregation, not a copy of the Mongo sync SQL CTE."""
    datasets = {
        "water": (
            "SELECT id_farm, date_trunc('month', registration_date)::date, "
            "SUM((end_hydrometer - start_hydrometer)::numeric) "
            "FROM public.water_registries GROUP BY 1, 2",
            "water_metrics", "monthly_total_m3",
        ),
        "energy": (
            "SELECT id_farm, date_trunc('month', registration_date)::date, "
            "SUM(energy_consumption::numeric) "
            "FROM public.energy_registries GROUP BY 1, 2",
            "energy_metrics", "monthly_total_kwh",
        ),
    }
    mongo_by_farm = {
        (int(doc["farm_id"]), date_value(doc["month_start"])): doc
        for doc in db[COLLECTION_MONTHLY].find({})
    }
    for name, (query, field, amount_field) in datasets.items():
        with source.cursor() as cursor:
            cursor.execute(query)
            data = cursor.fetchall()
        sql_totals = {(int(farm), month): numeric(amount) for farm, month, amount in data}
        for key, expected in sql_totals.items():
            doc = mongo_by_farm.get(key)
            if doc is None:
                raise AssertionError(f"{name}: missing monthly document for SQL registry")
            actual = numeric(doc[field][amount_field])
            if abs(expected - actual) > TOLERANCE:
                raise AssertionError(f"{name}: monthly total differs between SQL and Atlas")
        # Also check Atlas-only nonzero monthly totals without matching registries.
        for key, doc in mongo_by_farm.items():
            actual = numeric(doc[field][amount_field])
            if key not in sql_totals and abs(actual) > TOLERANCE:
                raise AssertionError(f"{name}: nonzero Atlas month without SQL source registry")
        print(f"MONTHLY TOTALS OK {name}: {len(sql_totals)} farm-month groups", flush=True)


def explain_payments(source, synced_payments: int) -> None:
    with source.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM public.payments")
        raw = cursor.fetchone()[0]
    print(
        f"PAYMENTS source_rows={raw} joined_analytics_rows={synced_payments}",
        flush=True,
    )
    if raw > 0 and synced_payments == 0:
        raise AssertionError("payments exist in production but none survived analytics joins")


def report_storage(db) -> None:
    total_bson_bytes = 0
    total_documents = 0
    for name in (*REPORT_COLLECTIONS, COLLECTION_STATE):
        count = 0
        bson_bytes = 0
        for doc in db[name].find({}):
            bson_bytes += len(BSON.encode(doc))
            count += 1
        total_bson_bytes += bson_bytes
        total_documents += count
        print(f"STORAGE {name}: documents={count} raw_BSON_bytes={bson_bytes}", flush=True)
    print(
        f"STORAGE total_documents={total_documents} raw_BSON_bytes={total_bson_bytes} "
        "(excludes indexes/Atlas overhead/compression)",
        flush=True,
    )
    try:
        stats = db.command({"dbStats": 1, "scale": 1})
        print(
            "ATLAS_DB_STATS "
            f"data_bytes={stats.get('dataSize')} "
            f"storage_bytes={stats.get('storageSize')} "
            f"index_bytes={stats.get('indexSize')} "
            f"total_bytes={stats.get('totalSize')}",
            flush=True,
        )
    except OperationFailure:
        print(
            "ATLAS_DB_STATS unavailable on current tier or role. "
            "Use Atlas Cluster Metrics to verify billed storage and free-tier headroom.",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Mongo POC safely")
    parser.add_argument("mode", choices=["snapshot", "verify"])
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()

    db_name = os.getenv("MONGODB_DATABASE", "ouros_analytics_poc")
    if not db_name.startswith("ouros_analytics_poc"):
        raise RuntimeError("Refusing to audit any database outside Mongo POC")
    client = mongodb_client()
    source = None
    try:
        db = client[db_name]
        client.admin.command("ping")
        state = db[COLLECTION_STATE].find_one({"_id": SYNC_ID})
        if not state or state.get("status") != "success":
            raise RuntimeError("MongoDB POC sync has not completed successfully")

        current = fingerprint(db)
        if args.mode == "snapshot":
            args.baseline.write_text(json.dumps(current, indent=2), encoding="utf-8")
            print(
                "IDEMPOTENCE baseline captured: " +
                ", ".join(f"{name}={v['count']}" for name, v in current.items()),
                flush=True,
            )
            return

        expected = json.loads(args.baseline.read_text(encoding="utf-8"))
        for name in REPORT_COLLECTIONS:
            if current.get(name) != expected.get(name):
                raise AssertionError(
                    f"IDEMPOTENCE FAILED {name}: two consecutive syncs changed stable business content"
                )
        print("IDEMPOTENCE OK: all document counts and business-content hashes unchanged", flush=True)

        run_id = state["run_id"]
        last_sync = state["last_sync"]
        source = connect_production_postgres()
        source.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        with source.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            if cursor.fetchone()[0] != "segundo_prod":
                raise RuntimeError("Verification is not connected to segundo_prod")

        parity = check_source_parity(source, db, run_id, last_sync)
        check_monthly_totals(source, db)
        explain_payments(source, parity["fact_payment"])
        report_storage(db)
        source.commit()
        print("POC AUDIT PASSED: idempotence, row parity and monthly totals", flush=True)
    finally:
        if source is not None:
            source.close()
        client.close()


if __name__ == "__main__":
    main()
