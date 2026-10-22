import unittest
from datetime import UTC, datetime
from decimal import Decimal

from bson import BSON, Decimal128

from scripts.audit_mongodb_poc import compare_documents, numeric, without_run


class Collection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, criteria):
        return [
            d for d in self.docs
            if all(d.get(key) == value for key, value in criteria.items())
        ]


class AuditMongoPocTest(unittest.TestCase):
    def test_compares_sql_and_mongo_timestamps_at_bson_precision(self):
        sql = {
            "_id": "test:1",
            "dataset": "demo",
            "date": datetime(2026, 10, 1, 9, 10, 12, 123456, tzinfo=UTC),
            "amount": Decimal128("2.000"),
            "_sync_run": "first",
        }
        mongo = BSON(BSON.encode(sql)).decode()
        mongo["_sync_run"] = "second"
        db = {"records": Collection([mongo])}
        self.assertEqual(
            compare_documents(db, "records", {"dataset": "demo"}, [sql], "demo"),
            1,
        )

    def test_mismatched_business_data_fails(self):
        db = {"records": Collection([{"_id": "k", "dataset": "demo", "value": 11}])}
        with self.assertRaisesRegex(AssertionError, "content differs"):
            compare_documents(
                db, "records", {"dataset": "demo"},
                [{"_id": "k", "dataset": "demo", "value": 12}], "demo",
            )

    def test_duplicate_source_ids_fail(self):
        db = {"records": Collection([])}
        source = [{"_id": "k", "dataset": "demo"}] * 2
        with self.assertRaisesRegex(AssertionError, "duplicate source"):
            compare_documents(db, "records", {"dataset": "demo"}, source, "demo")

    def test_unknown_source_ids_fail(self):
        db = {"records": Collection([])}
        with self.assertRaisesRegex(AssertionError, "expected 1 source IDs"):
            compare_documents(
                db, "records", {"dataset": "demo"},
                [{"_id": "k", "dataset": "demo"}], "demo",
            )

    def test_bson_decimal_is_lossless(self):
        self.assertEqual(numeric(Decimal128("123.1200")), Decimal("123.1200"))
        self.assertEqual(numeric(None), Decimal(0))
        self.assertEqual(without_run({"_sync_run": "r", "value": 2}), {"value": 2})


if __name__ == "__main__":
    unittest.main()
