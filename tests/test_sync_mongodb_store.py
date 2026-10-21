import unittest
from collections import defaultdict
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from bson import Decimal128

from scripts import sync_mongodb


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.indexes = []
        self.delete_calls = 0
        self.bulk_calls = 0

    def create_index(self, fields):
        self.indexes.append(fields)

    def bulk_write(self, operations, ordered=True):
        assert ordered
        self.bulk_calls += 1
        for operation in operations:
            self.rows[operation._filter["_id"]] = operation._doc

    def delete_many(self, selector):
        self.delete_calls += 1
        current = selector["_sync_run"]["$ne"]
        self.rows = {
            key: row for key, row in self.rows.items()
            if row["_sync_run"] == current
        }


class FakeDatabase(defaultdict):
    def __init__(self):
        super().__init__(FakeCollection)


class MongoWriterIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.source_rows = {
            "mongo_dim_enterprise": [(7, "Empresa", "SP", "Sao Paulo")],
            "mongo_dim_farm": [
                (42, 7, "Empresa", "Fazenda", "sp", "Capital",
                 Decimal("20"), 100, 23),
            ],
            "mongo_fact_farm_consumption_monthly": [
                (date(2026, 10, 1), 42, 7, "Fazenda", "sp",
                 23, Decimal("3.5"), Decimal("12.4")),
            ],
            "mongo_feedback": [(5, 8, 42, "Fazenda", 4, ["sustentabilidade"])],
            "mongo_water_readings": [
                (10, 42, 7, "Fazenda", "sp", date(2026, 10, 2), Decimal("3.5")),
            ],
            "mongo_energy_readings": [
                (20, 42, 7, date(2026, 10, 3), Decimal("12.4")),
            ],
        }

    def stream(self, _source, _query, params=None, *, name):
        return iter(self.source_rows.get(name, []))

    def test_two_runs_upsert_and_reconcile_hard_deletes(self):
        with patch.object(sync_mongodb, "stream_rows", side_effect=self.stream):
            first = sync_mongodb.sync_snapshot(
                object(), self.db, "first", sync_mongodb.EPOCH,
            )
            self.assertEqual(first["dim_farm"], 1)
            self.assertEqual(first["water_buckets"], 1)
            self.assertEqual(first["energy_buckets"], 1)
            self.assertEqual(first["fact_tip_feedback"], 1)
            self.assertEqual(len(self.db[sync_mongodb.COLLECTION_BUCKETS].rows), 2)
            self.assertEqual(len(self.db[sync_mongodb.COLLECTION_MONTHLY].rows), 1)

            # Historical water correction, energy deletion and feedback unlink.
            self.source_rows["mongo_water_readings"] = [
                (10, 42, 7, "Fazenda", "sp", date(2026, 10, 2), Decimal("9.5")),
            ]
            self.source_rows["mongo_energy_readings"] = []
            self.source_rows["mongo_feedback"] = []
            second = sync_mongodb.sync_snapshot(
                object(), self.db, "second", sync_mongodb.EPOCH,
            )
        self.assertEqual(second["energy_buckets"], 0)
        buckets = self.db[sync_mongodb.COLLECTION_BUCKETS].rows
        self.assertEqual(len(buckets), 1)
        self.assertEqual(
            buckets["water:42:2026-10:0000"]["readings"][0]["value"],
            Decimal128("9.5"),
        )
        records = self.db[sync_mongodb.COLLECTION_ROWS].rows
        self.assertNotIn("fact_tip_feedback:5:42", records)
        self.assertIn("dim_farm:42", records)
        self.assertEqual(len(self.db[sync_mongodb.COLLECTION_MONTHLY].rows), 1)

    def test_failed_source_read_does_not_prune_old_collections(self):
        with patch.object(sync_mongodb, "stream_rows", side_effect=self.stream):
            sync_mongodb.sync_snapshot(object(), self.db, "good", sync_mongodb.EPOCH)
        before = {
            name: collection.delete_calls for name, collection in self.db.items()
        }

        def failing_stream(_source, _query, params=None, *, name):
            if name == "mongo_energy_readings":
                raise RuntimeError("source broke")
            return iter(self.source_rows.get(name, []))

        with patch.object(sync_mongodb, "stream_rows", side_effect=failing_stream):
            with self.assertRaisesRegex(RuntimeError, "source broke"):
                sync_mongodb.sync_snapshot(object(), self.db, "bad", sync_mongodb.EPOCH)
        for name in sync_mongodb.COLLECTIONS:
            self.assertEqual(self.db[name].delete_calls, before[name])

    def test_bulk_writer_batches_and_is_idempotent_by_identifier(self):
        coll = FakeCollection()
        docs = [{"_id": str(i), "_sync_run": "sync"} for i in range(503)]
        self.assertEqual(sync_mongodb.write_docs(coll, iter(docs)), 503)
        self.assertEqual(coll.bulk_calls, 2)
        self.assertEqual(len(coll.rows), 503)
        self.assertEqual(sync_mongodb.write_docs(coll, iter(docs)), 503)
        self.assertEqual(len(coll.rows), 503)
        self.assertEqual(sync_mongodb.write_docs(coll, iter([])), 0)


if __name__ == "__main__":
    unittest.main()
