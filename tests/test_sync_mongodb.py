import os
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from bson import Decimal128

from scripts import sync_mongodb


class EnforcedTransportTest(unittest.TestCase):
    def test_postgres_defaults_to_verify_full_with_system_ca(self):
        with patch.dict(os.environ, {
            "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/segundo_prod",
        }, clear=True), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=object(),
        ) as connect:
            sync_mongodb.connect_production_postgres()
        self.assertEqual(connect.call_args.kwargs["sslmode"], "verify-full")
        self.assertEqual(connect.call_args.kwargs["sslrootcert"], "system")

    def test_postgres_explicit_verify_full_uses_trusted_ca(self):
        with patch.dict(os.environ, {
            "PRODUCTION_DATABASE_URL": (
                "postgresql://example.invalid/segundo_prod?sslmode=verify-full"
            ),
        }, clear=True), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=object(),
        ) as connect:
            sync_mongodb.connect_production_postgres()
        self.assertEqual(connect.call_args.kwargs["sslmode"], "verify-full")
        self.assertEqual(connect.call_args.kwargs["sslrootcert"], "system")

    def test_postgres_explicit_ca_is_preserved(self):
        with patch.dict(os.environ, {
            "PRODUCTION_DATABASE_URL": (
                "postgresql://example.invalid/segundo_prod?"
                "sslmode=verify-full&sslrootcert=%2Ftmp%2Froot.pem"
            ),
        }, clear=True), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=object(),
        ) as connect:
            sync_mongodb.connect_production_postgres()
        self.assertEqual(connect.call_args.kwargs, {"sslmode": "verify-full"})

    def test_postgres_ca_environment_is_preserved(self):
        with patch.dict(os.environ, {
            "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/segundo_prod",
            "PGSSLROOTCERT": "/tmp/test-trusted-ca.pem",
        }, clear=True), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=object(),
        ) as connect:
            sync_mongodb.connect_production_postgres()
        self.assertEqual(connect.call_args.kwargs, {"sslmode": "verify-full"})

    def test_postgres_rejects_non_verified_ssl_modes(self):
        for mode in ("disable", "allow", "prefer", "require", "verify-ca"):
            with self.subTest(mode=mode), patch.dict(os.environ, {
                "PRODUCTION_DATABASE_URL": (
                    "postgresql://example.invalid/segundo_prod?sslmode=" + mode
                ),
            }, clear=True), patch.object(
                sync_mongodb.psycopg2, "connect",
            ) as connect:
                with self.assertRaisesRegex(RuntimeError, "sslmode=verify-full"):
                    sync_mongodb.connect_production_postgres()
                connect.assert_not_called()

    def test_mongo_clients_enforce_tls_even_for_standard_uri(self):
        with patch.dict(os.environ, {
            "MONGODB_URI": "mongodb://example.invalid:27017",
        }, clear=True), patch.object(
            sync_mongodb, "MongoClient", return_value=object(),
        ) as create:
            sync_mongodb.mongodb_client(retry_writes=True)
            self.assertTrue(create.call_args.kwargs["tls"])
            self.assertTrue(create.call_args.kwargs["retryWrites"])
            sync_mongodb.mongodb_client()
            self.assertTrue(create.call_args.kwargs["tls"])
            self.assertNotIn("retryWrites", create.call_args.kwargs)

    def test_mongo_allows_explicit_secure_uri_options(self):
        with patch.dict(os.environ, {
            "MONGODB_URI": (
                "mongodb+srv://example.invalid/?tlsInsecure=false"
                "&tlsAllowInvalidHostnames=false&tls=true"
            ),
        }, clear=True), patch.object(
            sync_mongodb, "MongoClient", return_value=object(),
        ) as create:
            sync_mongodb.mongodb_client()
            create.assert_called_once()

    def test_mongo_rejects_insecure_tls_uri_options(self):
        unsafe_parameters = (
            "tlsInsecure=true",
            "tlsAllowInvalidCertificates=true",
            "tlsAllowInvalidHostnames=true",
            "TLSINSECURE=%74rue",
            "tls=false",
            "ssl=false",
        )
        for parameter in unsafe_parameters:
            with self.subTest(parameter=parameter), patch.dict(os.environ, {
                "MONGODB_URI": f"mongodb://example.invalid/?{parameter}",
            }, clear=True), patch.object(sync_mongodb, "MongoClient") as create:
                with self.assertRaisesRegex(RuntimeError, "MONGODB_URI"):
                    sync_mongodb.mongodb_client()
                create.assert_not_called()

    def test_mongo_cannot_enable_unsafe_duplicate_option(self):
        with patch.dict(os.environ, {
            "MONGODB_URI": (
                "mongodb://example.invalid/?tlsInsecure=false&tlsInsecure=true"
            ),
        }, clear=True), patch.object(sync_mongodb, "MongoClient") as create:
            with self.assertRaisesRegex(RuntimeError, "insecure TLS"):
                sync_mongodb.mongodb_client()
            create.assert_not_called()


class MongoModelsTest(unittest.TestCase):
    def test_document_keys_and_decimal_are_stable(self):
        doc = sync_mongodb.document_for(
            "fact_lot", ("lot_id", "farm_id", "cost"), ("lot_id",),
            (77, 12, Decimal("19.99")), "run1",
        )
        self.assertEqual(doc["_id"], "fact_lot:77")
        self.assertEqual(doc["farm_id"], 12)
        self.assertEqual(doc["cost"], Decimal128("19.99"))
        self.assertEqual(doc["_sync_run"], "run1")

    def test_shared_state_goal_has_distinct_ids_for_different_farms(self):
        columns = ("goal_id", "goal_scope", "farm_id", "goal_status")
        first = sync_mongodb.document_for(
            "fact_goal", columns, ("goal_scope", "goal_id"), (12, "state", 42, "Pendente"), "run",
        )
        second = sync_mongodb.document_for(
            "fact_goal", columns, ("goal_scope", "goal_id"), (12, "state", 43, "Concluída"), "run",
        )
        self.assertNotEqual(first["_id"], second["_id"])
        self.assertEqual(first["_id"], "fact_goal:state:12:42")
        self.assertEqual(second["_id"], "fact_goal:state:12:43")

    def test_monthly_snapshot_is_partitioned_by_farm_and_month(self):
        doc = sync_mongodb.document_for(
            "fact_farm_consumption_monthly",
            ("month_start", "farm_id", "enterprise_id", "water_consumed_m3",
             "energy_consumed_kwh", "chickens_reference"),
            ("month_start", "farm_id"),
            (date(2026, 10, 1), 42, 7, Decimal("2.300"), Decimal("4.100"), 12),
            "run2",
        )
        self.assertEqual(doc["_id"], "42:2026-10-01")
        self.assertEqual(doc["water_metrics"]["monthly_total_m3"], Decimal128("2.300"))
        self.assertEqual(doc["energy_metrics"]["monthly_total_kwh"], Decimal128("4.100"))

    def test_water_buckets_split_before_limit(self):
        readings = [
            (idx, 42, 7, "Farm", "sp", date(2026, 10, idx), Decimal("1.2"))
            for idx in range(1, 6)
        ]
        buckets = list(sync_mongodb.measurement_buckets(
            "water", readings, "run3", max_readings=2,
        ))
        self.assertEqual([len(d["readings"]) for d in buckets], [2, 2, 1])
        self.assertEqual(
            [d["_id"] for d in buckets],
            ["water:42:2026-10:0000", "water:42:2026-10:0001",
             "water:42:2026-10:0002"],
        )
        self.assertEqual(buckets[0]["readings"][0]["source_id"], 1)
        self.assertEqual(buckets[0]["readings"][0]["value"], Decimal128("1.2"))

    def test_bucket_resets_chunk_for_new_farm_and_month(self):
        rows = [
            (1, 42, 7, date(2026, 10, 1), Decimal("3")),
            (2, 42, 7, date(2026, 11, 1), Decimal("4")),
            (3, 43, 7, date(2026, 11, 2), Decimal("5")),
        ]
        ids = [d["_id"] for d in sync_mongodb.measurement_buckets(
            "energy", rows, "run4", max_readings=1,
        )]
        self.assertEqual(ids, [
            "energy:42:2026-10:0000",
            "energy:42:2026-11:0000",
            "energy:43:2026-11:0000",
        ])

    def test_oversized_document_is_rejected(self):
        with patch.object(sync_mongodb, "MAX_BSON_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "12 MiB"):
                list(sync_mongodb.measurement_buckets(
                    "energy", [(1, 42, 7, date(2026, 10, 1), 2)], "run",
                ))

    def test_required_secrets_and_poc_database_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MONGODB_URI"):
                sync_mongodb.main()
        with patch.dict(os.environ, {
            "MONGODB_URI": "mongodb://example.invalid/",
            "MONGODB_DATABASE": "production",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "POC safety"):
                sync_mongodb.main()


class FakeSourceCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        pass

    def fetchone(self):
        return (datetime(2026, 10, 21, tzinfo=UTC),)


class FakeSource:
    def __init__(self):
        self.rolled_back = False
        self.committed = False

    def set_session(self, **_kwargs):
        pass

    def cursor(self):
        return FakeSourceCursor()

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def close(self):
        pass


class FakeState:
    def __init__(self):
        self.operations = []

    def update_one(self, selector, values, **_kwargs):
        self.operations.append((selector, values))


class FakeClient:
    def __init__(self, state):
        self.admin = self
        self.state = state

    def command(self, *_args):
        pass

    def __getitem__(self, _db_name):
        return {"poc_sync_state": self.state}

    def close(self):
        pass


class MongoSyncLifecycleTest(unittest.TestCase):
    def test_failure_keeps_previous_checkpoint_and_reports_failure(self):
        state, source = FakeState(), FakeSource()
        with patch.dict(os.environ, {
            "MONGODB_URI": "mongodb://example.invalid/",
            "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/",
        }, clear=True), patch.object(
            sync_mongodb, "MongoClient", return_value=FakeClient(state),
        ), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=source,
        ), patch.object(
            sync_mongodb, "sync_snapshot", side_effect=RuntimeError("test crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "test crash"):
                sync_mongodb.main()
        self.assertTrue(source.rolled_back)
        self.assertFalse(source.committed)
        updates = [update["$set"] for _, update in state.operations]
        self.assertEqual([update["status"] for update in updates], ["running", "failed"])
        self.assertFalse(any("last_sync" in update for update in updates))

    def test_success_advances_checkpoint_once(self):
        state, source = FakeState(), FakeSource()
        with patch.dict(os.environ, {
            "MONGODB_URI": "mongodb://example.invalid/",
            "PRODUCTION_DATABASE_URL": "postgresql://example.invalid/",
        }, clear=True), patch.object(
            sync_mongodb, "MongoClient", return_value=FakeClient(state),
        ), patch.object(
            sync_mongodb.psycopg2, "connect", return_value=source,
        ), patch.object(
            sync_mongodb, "sync_snapshot", return_value={"dim_farm": 3},
        ):
            sync_mongodb.main()
        self.assertTrue(source.committed)
        updates = [update["$set"] for _, update in state.operations]
        self.assertEqual([update["status"] for update in updates], ["running", "success"])
        self.assertEqual(updates[-1]["counts"], {"dim_farm": 3})
        self.assertEqual(updates[-1]["last_sync"], datetime(2026, 10, 21, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
