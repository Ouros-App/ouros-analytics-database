import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from bson import BSON, Decimal128
from pymongo.errors import OperationFailure

from scripts import audit_mongodb_poc as audit
from scripts.sync_mongodb import document_for


class Cursor:
    def __init__(self, water=None, energy=None, payments=0):
        self.query = ""
        self.water = water or []
        self.energy = energy or []
        self.payments = payments

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        self.query = query

    def fetchall(self):
        if "FROM public.water_registries" in self.query:
            return self.water
        if "FROM public.energy_registries" in self.query:
            return self.energy
        return []

    def fetchone(self):
        if "COUNT(*) FROM public.payments" in self.query:
            return (self.payments,)
        if "current_database()" in self.query:
            return ("segundo_prod",)
        return (None,)


class Source:
    def __init__(self, water=None, energy=None, payments=0):
        self.water = water
        self.energy = energy
        self.payments = payments
        self.committed = False
        self.closed = False

    def cursor(self):
        return Cursor(self.water, self.energy, self.payments)

    def set_session(self, **_kwargs):
        pass

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class Collection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, filter_by):
        return [
            document for document in self.docs
            if all(document.get(key) == value for key, value in filter_by.items())
        ]

    def find_one(self, selector):
        return next((d for d in self.docs if d.get("_id") == selector["_id"]), None)


class Database(dict):
    def __init__(self, *, fail_stats=False):
        super().__init__()
        self.fail_stats = fail_stats

    def __getitem__(self, key):
        return super().get(key, Collection([]))

    def command(self, _command):
        if self.fail_stats:
            raise OperationFailure("not supported", code=13)
        return {
            "dataSize": 500, "storageSize": 300,
            "indexSize": 250, "totalSize": 550,
        }


class Client:
    def __init__(self, db):
        self.db = db
        self.admin = self
        self.closed = False

    def __getitem__(self, _name):
        return self.db

    def command(self, _name):
        return {"ok": 1}

    def close(self):
        self.closed = True


class AuditScenariosTest(unittest.TestCase):
    def setUp(self):
        self.sql_month = date(2026, 10, 1)
        self.month = datetime(2026, 10, 1, tzinfo=UTC)
        self.db = Database()
        self.db[audit.COLLECTION_MONTHLY] = Collection([
            {
                "_id": "42:2026-10-01", "farm_id": 42, "month_start": self.month,
                "water_metrics": {"monthly_total_m3": Decimal128("3.5")},
                "energy_metrics": {"monthly_total_kwh": Decimal128("12.4")},
            }
        ])

    def test_monthly_independent_sql_totals_match(self):
        source = Source(
            water=[(42, self.sql_month, Decimal("3.5"))],
            energy=[(42, self.sql_month, Decimal("12.4"))],
        )
        audit.check_monthly_totals(source, self.db)

    def test_monthly_missing_group_fails(self):
        source = Source(water=[(43, self.sql_month, Decimal("3.5"))])
        with self.assertRaisesRegex(AssertionError, "missing monthly document"):
            audit.check_monthly_totals(source, self.db)

    def test_monthly_wrong_amount_fails(self):
        source = Source(water=[(42, self.sql_month, Decimal("999"))])
        with self.assertRaisesRegex(AssertionError, "monthly total differs"):
            audit.check_monthly_totals(source, self.db)

    def test_monthly_atlas_only_nonzero_fails(self):
        with self.assertRaisesRegex(AssertionError, "nonzero Atlas month"):
            audit.check_monthly_totals(Source(), self.db)

    def test_zero_payments_are_valid(self):
        audit.explain_payments(Source(payments=0), 0)
        audit.explain_payments(Source(payments=12), 12)

    def test_payments_disappeared_in_join_are_rejected(self):
        with self.assertRaisesRegex(AssertionError, "none survived"):
            audit.explain_payments(Source(payments=1), 0)

    def test_storage_reports_index_and_data_bytes(self):
        self.db[audit.COLLECTION_ROWS] = Collection([
            {"_id": "test", "dataset": "dim_farm", "farm_id": 1}
        ])
        self.db[audit.COLLECTION_STATE] = Collection([{"_id": "state"}])
        audit.report_storage(self.db)
        self.db.fail_stats = True
        audit.report_storage(self.db)

    def test_parity_can_stream_facts_and_empty_metrics(self):
        cols = ("enterprise_id", "enterprise_name")
        row = (7, "Empresa")
        rendered = document_for("dim_enterprise", cols, ("enterprise_id",), row, "run")
        self.db[audit.COLLECTION_ROWS] = Collection([BSON(BSON.encode(rendered)).decode()])
        self.db[audit.COLLECTION_BUCKETS] = Collection([])
        self.db[audit.COLLECTION_MONTHLY] = Collection([])

        def rows(_source, _query, params=None, *, name):
            return iter([row] if name == "audit_dim_enterprise" else [])
        with patch.object(
            audit, "STEPS",
            [("dim_enterprise", "SELECT fake", cols, ("enterprise_id",))],
        ), patch.object(audit, "stream_rows", side_effect=rows):
            counts = audit.check_source_parity(
                Source(), self.db, "run", datetime(2026, 10, 22, tzinfo=UTC)
            )
        self.assertEqual(counts["dim_enterprise"], 1)
        self.assertEqual(counts["water_buckets"], 0)
        self.assertEqual(counts["fact_tip_feedback"], 0)

    def test_snapshot_and_verify_entrypoint(self):
        state = {
            "_id": audit.SYNC_ID,
            "status": "success",
            "run_id": "run-2",
            "last_sync": datetime(2026, 10, 22, tzinfo=UTC),
        }
        self.db[audit.COLLECTION_STATE] = Collection([state])
        client = Client(self.db)
        before = {"poc_analytics_rows": {"count": 1, "sha256": "abc"},
                  "poc_farm_monthly": {"count": 1, "sha256": "def"},
                  "poc_metric_buckets": {"count": 1, "sha256": "ghi"}}
        source = Source()
        with tempfile.TemporaryDirectory() as folder:
            baseline = str(Path(folder) / "baseline.json")
            with patch.dict(os.environ, {
                "MONGODB_URI": "mongodb://example.invalid/",
                "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/",
            }), patch.object(audit, "mongodb_client", return_value=client), patch.object(
                audit, "fingerprint", return_value=before,
            ), patch.object(
                sys, "argv", ["audit", "snapshot", "--baseline", baseline]
            ):
                audit.main()
            self.assertEqual(json.loads(Path(baseline).read_text()), before)
            with patch.dict(os.environ, {
                "MONGODB_URI": "mongodb://example.invalid/",
                "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/",
            }), patch.object(audit, "mongodb_client", return_value=client), patch.object(
                audit, "fingerprint", return_value=before,
            ), patch.object(
                audit, "connect_production_postgres", return_value=source,
            ), patch.object(
                audit, "check_source_parity", return_value={"fact_payment": 0}
            ) as parity, patch.object(
                audit, "check_monthly_totals"
            ) as monthly, patch.object(
                audit, "explain_payments"
            ) as payments, patch.object(
                audit, "report_storage"
            ) as storage, patch.object(
                sys, "argv", ["audit", "verify", "--baseline", baseline]
            ):
                audit.main()
            parity.assert_called_once()
            monthly.assert_called_once()
            payments.assert_called_once()
            storage.assert_called_once()
            self.assertTrue(source.committed)
            self.assertTrue(source.closed)

    def test_verify_fails_when_second_snapshot_differs(self):
        state = {"_id": audit.SYNC_ID, "status": "success", "run_id": "run"}
        self.db[audit.COLLECTION_STATE] = Collection([state])
        client = Client(self.db)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "baseline.json"
            path.write_text(json.dumps({
                "poc_analytics_rows": {"count": 1, "sha256": "old"},
                "poc_farm_monthly": {"count": 0, "sha256": "x"},
                "poc_metric_buckets": {"count": 0, "sha256": "y"},
            }))
            with patch.dict(os.environ, {"MONGODB_URI": "mongodb://invalid"}), patch.object(
                audit, "mongodb_client", return_value=client
            ), patch.object(
                audit, "fingerprint", return_value={
                    "poc_analytics_rows": {"count": 2, "sha256": "changed"}
                }
            ), patch.object(
                sys, "argv", ["audit", "verify", "--baseline", str(path)]
            ):
                with self.assertRaisesRegex(AssertionError, "IDEMPOTENCE FAILED"):
                    audit.main()


if __name__ == "__main__":
    unittest.main()
