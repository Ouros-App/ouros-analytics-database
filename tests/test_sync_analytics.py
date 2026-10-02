import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts import sync_analytics


class FakeCursor:
    def __init__(self, role, rows=None, *, stream=False, registry_exists=False):
        self.role = role
        self.rows = list(rows or [])
        self.stream = stream
        self.registry_exists = registry_exists
        self.pending = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        self.executed.append(query)
        if self.role == "target" and query.startswith("SELECT last_sync"):
            self.pending = "last_sync"
        elif self.role == "target" and query.startswith(
            "SELECT EXISTS (SELECT 1 FROM analytics.fact_water_registry"
        ):
            self.pending = "registry_exists"
        elif self.role == "source" and query == "SELECT CURRENT_TIMESTAMP":
            self.pending = "sync_end"
        elif self.role == "source":
            self.pending = "rows"

    def fetchone(self):
        if self.pending == "last_sync":
            return (datetime(2026, 1, 1, tzinfo=timezone.utc),)
        if self.pending == "sync_end":
            return (datetime(2026, 9, 16, tzinfo=timezone.utc),)
        if self.pending == "registry_exists":
            return (self.registry_exists,)
        raise AssertionError(f"fetchone inesperado: {self.pending}")

    def fetchall(self):
        if self.pending != "rows":
            raise AssertionError(f"fetchall inesperado: {self.pending}")
        return self.rows.pop(0) if self.rows else []

    def fetchmany(self, size):
        if self.pending != "rows" or not self.stream:
            raise AssertionError(f"fetchmany inesperado: {self.pending}")
        batch = self.rows[:size]
        self.rows = self.rows[size:]
        return batch


class FakeConnection:
    def __init__(self, role, rows=None, *, named_rows=None, registry_exists=False):
        self.cursor_obj = FakeCursor(role, rows)
        self.named_rows = list(named_rows or [])
        self.named_cursors = []
        self.registry_exists = registry_exists
        self.cursor_obj.registry_exists = registry_exists
        self.committed = False
        self.rolled_back = False

    def set_session(self, **_kwargs):
        return None

    def cursor(self, name=None):
        if name is None:
            return self.cursor_obj
        cursor = FakeCursor("source", self.named_rows.pop(0), stream=True)
        self.named_cursors.append(cursor)
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False

    def commit(self):
        self.committed = True

    def close(self):
        return None


class SyncAnalyticsTest(unittest.TestCase):
    def test_env_requires_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                sync_analytics.env("DATABASE_URL")

    def test_main_upserts_and_commits_watermark(self) -> None:
        source = FakeConnection(
            "source",
            [[(1, "Empresa", "SP", "Sao Paulo")], [], [], [], [], [], []],
            named_rows=[[], []],
        )
        target = FakeConnection("target")
        execute_calls = []

        with patch.dict(
            os.environ,
            {
                "PRODUCTION_DATABASE_URL": "postgresql://source",
                "ANALYTICS_SYNC_DATABASE_URL": "postgresql://target",
            },
            clear=True,
        ), patch.object(sync_analytics.psycopg2, "connect", side_effect=[source, target]), patch.object(
            sync_analytics, "execute_values", side_effect=lambda _cur, query, rows, page_size: execute_calls.append(query)
        ):
            sync_analytics.main()

        self.assertTrue(target.committed)
        self.assertTrue(source.committed)
        self.assertTrue(any("analytics.dim_enterprise" in query for query in execute_calls))
        self.assertTrue(any("UPDATE analytics.sync_state" in query for query in target.cursor_obj.executed))

    def test_water_registry_fact_backfills_once_in_batches_and_reconciles_deletes(self) -> None:
        """The initial load streams full history and removes stale destination keys."""
        reading = (1, 3, 8, "Farm", "sp", "2026-09-04", 540)
        source = FakeConnection(
            "source",
            named_rows=[[(1,), (2,)], [reading]],
        )
        target = FakeCursor("target", registry_exists=False)
        upsert_calls = []

        with patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, rows, page_size: upsert_calls.append(
                (query, rows, page_size)
            ),
        ):
            count = sync_analytics.sync_water_registry_fact(
                source,
                target,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 16, tzinfo=timezone.utc),
            )

        self.assertEqual(count, 1)
        self.assertIn(
            sync_analytics.WATER_REGISTRY_KEYS_QUERY,
            source.named_cursors[0].executed,
        )
        self.assertEqual(source.named_cursors[1].executed, [sync_analytics.WATER_REGISTRY_QUERY])
        self.assertTrue(
            any("DELETE FROM analytics.fact_water_registry" in query for query in target.executed)
        )
        self.assertEqual(len(upsert_calls), 2)
        self.assertEqual(upsert_calls[1][1], [reading])

    def test_water_registry_fact_only_reads_changes_after_initial_load(self) -> None:
        """Subsequent cycles stream changed rows instead of the full history."""
        reading = (3, 4, 9, "Other Farm", "sp", "2026-09-15", 25)
        source = FakeConnection("source", named_rows=[[(3,)], [reading]])
        target = FakeCursor("target", registry_exists=True)
        upsert_calls = []

        with patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, rows, page_size: upsert_calls.append(
                (query, rows, page_size)
            ),
        ):
            count = sync_analytics.sync_water_registry_fact(
                source,
                target,
                datetime(2026, 9, 14, tzinfo=timezone.utc),
                datetime(2026, 9, 16, tzinfo=timezone.utc),
            )

        self.assertEqual(count, 1)
        self.assertEqual(
            source.named_cursors[1].executed,
            [sync_analytics.WATER_REGISTRY_INCREMENTAL_QUERY],
        )
        self.assertIn(
            "w.updated_at > b.last_sync",
            sync_analytics.WATER_REGISTRY_INCREMENTAL_QUERY,
        )
        self.assertEqual(upsert_calls[-1][1], [reading])


    def test_tip_feedback_snapshot_uses_normalized_farm_relation(self) -> None:
        """The snapshot must follow the normalized tip-to-farm relationship."""
        query = sync_analytics.TIP_FEEDBACK_SNAPSHOT_QUERY

        self.assertIn("JOIN public.farms_tips ft ON ft.id_tip = t.id", query)
        self.assertIn("JOIN public.farms f ON f.id = ft.id_farm", query)
        self.assertNotIn("JOIN public.farms f ON f.id = t.id_farm", query)
        self.assertNotIn("updated_at", query)
        self.assertEqual(
            sync_analytics.TIP_FEEDBACK_CONFLICT,
            ("review_id", "farm_id"),
        )

    def test_tip_feedback_snapshot_reconciles_removed_links(self) -> None:
        """Current source keys are staged before one stale-row reconciliation."""
        source = FakeCursor(
            "source",
            [[(7, 10, 3, "Farm", 5, ["sustentabilidade"])]],
        )
        target = FakeCursor("target")
        insert_calls = []

        with patch.object(
            sync_analytics,
            "upsert",
            return_value=1,
        ), patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, rows, page_size: insert_calls.append(
                (query, rows, page_size)
            ),
        ):
            count = sync_analytics.sync_tip_feedback_snapshot(source, target)

        self.assertEqual(count, 1)
        self.assertEqual(len(insert_calls), 1)
        query, rows, page_size = insert_calls[0]
        self.assertIn("INSERT INTO tip_feedback_current_keys", query)
        self.assertEqual(rows, [(7, 3)])
        self.assertEqual(page_size, 1000)
        self.assertTrue(
            any("CREATE TEMP TABLE tip_feedback_current_keys" in query for query in target.executed)
        )
        self.assertTrue(
            any(
                "DELETE FROM analytics.fact_tip_feedback" in query
                and "tip_feedback_current_keys" in query
                for query in target.executed
            )
        )

    def test_tip_feedback_snapshot_handles_more_than_one_page_of_keys(self) -> None:
        """Paging the temp-table insert must not split stale-row deletion."""
        rows = [
            (review_id, 10, review_id, f"Farm {review_id}", 5, [])
            for review_id in range(1, 1002)
        ]
        source = FakeCursor("source", [rows])
        target = FakeCursor("target")
        insert_calls = []

        with patch.object(
            sync_analytics,
            "upsert",
            return_value=len(rows),
        ), patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, values, page_size: insert_calls.append(
                (query, values, page_size)
            ),
        ):
            count = sync_analytics.sync_tip_feedback_snapshot(source, target)

        self.assertEqual(count, 1001)
        self.assertEqual(len(insert_calls), 1)
        query, staged_keys, page_size = insert_calls[0]
        self.assertIn("INSERT INTO tip_feedback_current_keys", query)
        self.assertEqual(len(staged_keys), 1001)
        self.assertEqual(page_size, 1000)
        delete_queries = [
            query
            for query in target.executed
            if "DELETE FROM analytics.fact_tip_feedback" in query
        ]
        self.assertEqual(len(delete_queries), 1)
        self.assertIn("tip_feedback_current_keys", delete_queries[0])
        self.assertNotIn("VALUES %s", delete_queries[0])

    def test_tip_feedback_snapshot_empty_source_clears_destination(self) -> None:
        """An empty source snapshot removes all stale feedback rows."""
        source = FakeCursor("source", [[]])
        target = FakeCursor("target")

        with patch.object(sync_analytics, "upsert", return_value=0):
            count = sync_analytics.sync_tip_feedback_snapshot(source, target)

        self.assertEqual(count, 0)
        self.assertTrue(
            any(
                query == "DELETE FROM analytics.fact_tip_feedback"
                for query in target.executed
            )
        )

    def test_failed_step_rolls_back_without_advancing_watermark(self) -> None:
        source = FakeConnection("source", [[(1, "Empresa", "SP", "Sao Paulo")]])
        target = FakeConnection("target")

        with patch.dict(
            os.environ,
            {
                "PRODUCTION_DATABASE_URL": "postgresql://source",
                "ANALYTICS_SYNC_DATABASE_URL": "postgresql://target",
            },
            clear=True,
        ), patch.object(sync_analytics.psycopg2, "connect", side_effect=[source, target]), patch.object(
            sync_analytics, "upsert", side_effect=RuntimeError("falha")
        ):
            with self.assertRaisesRegex(RuntimeError, "falha"):
                sync_analytics.main()

        self.assertTrue(target.rolled_back)
        self.assertFalse(target.committed)
        self.assertFalse(any("UPDATE analytics.sync_state" in query for query in target.cursor_obj.executed))


if __name__ == "__main__":
    unittest.main()
