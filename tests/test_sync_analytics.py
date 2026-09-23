import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts import sync_analytics


class FakeCursor:
    def __init__(self, role, rows=None):
        self.role = role
        self.rows = list(rows or [])
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
        elif self.role == "source" and query == "SELECT CURRENT_TIMESTAMP":
            self.pending = "sync_end"
        elif query.lstrip().startswith("SELECT") or self.role == "source":
            self.pending = "rows"

    def fetchone(self):
        if self.pending == "last_sync":
            return (datetime(2026, 1, 1, tzinfo=timezone.utc),)
        if self.pending == "sync_end":
            return (datetime(2026, 9, 16, tzinfo=timezone.utc),)
        raise AssertionError(f"fetchone inesperado: {self.pending}")

    def fetchall(self):
        if self.pending != "rows":
            raise AssertionError(f"fetchall inesperado: {self.pending}")
        return self.rows.pop(0) if self.rows else []


class FakeConnection:
    def __init__(self, role, rows=None):
        self.cursor_obj = FakeCursor(role, rows)
        self.committed = False
        self.rolled_back = False

    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return self.cursor_obj

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

    def test_delete_rows_uses_composite_key(self) -> None:
        cursor = object()
        calls = []

        with patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, rows, page_size: calls.append(
                (query, rows, page_size)
            ),
        ):
            deleted = sync_analytics.delete_rows(
                cursor,
                "analytics.fact_goal",
                ("goal_scope", "goal_id"),
                [("state", 7)],
            )

        self.assertEqual(deleted, 1)
        query, rows, page_size = calls[0]
        self.assertIn("DELETE FROM analytics.fact_goal AS target", query)
        self.assertIn("target.goal_scope = stale.goal_scope", query)
        self.assertIn("target.goal_id = stale.goal_id", query)
        self.assertEqual(rows, [("state", 7)])
        self.assertEqual(page_size, 1000)

    def test_reconcile_deleted_rows_removes_only_stale_keys(self) -> None:
        source = FakeCursor("source", [[(1,)]])
        target = FakeCursor("target", [[(1,), (2,)]])
        delete_calls = []

        with patch.object(
            sync_analytics,
            "RECONCILIATIONS",
            [
                (
                    "dim_enterprise",
                    ("enterprise_id",),
                    "SELECT id FROM public.enterprises",
                )
            ],
        ), patch.object(
            sync_analytics,
            "delete_rows",
            side_effect=lambda _cur, table, columns, keys: (
                delete_calls.append((table, columns, set(keys))) or len(keys)
            ),
        ):
            counts = sync_analytics.reconcile_deleted_rows(source, target)

        self.assertEqual(counts["deleted_dim_enterprise"], 1)
        self.assertEqual(
            delete_calls,
            [
                (
                    "analytics.dim_enterprise",
                    ("enterprise_id",),
                    {(2,)},
                )
            ],
        )

    def test_main_upserts_refreshes_reconciles_and_commits_watermark(self) -> None:
        source = FakeConnection(
            "source",
            [[(1, "Empresa", "SP", "Sao Paulo")], [], [], [], [], [], []],
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
        ), patch.object(
            sync_analytics.psycopg2,
            "connect",
            side_effect=[source, target],
        ), patch.object(
            sync_analytics,
            "execute_values",
            side_effect=lambda _cur, query, rows, page_size: execute_calls.append(
                query
            ),
        ), patch.object(
            sync_analytics,
            "refresh_derived_rows",
            return_value={"refreshed_fact_tip_feedback": 0},
        ) as refresh, patch.object(
            sync_analytics,
            "reconcile_deleted_rows",
            return_value={"deleted_fact_tip_feedback": 0},
        ) as reconcile:
            sync_analytics.main()

        self.assertTrue(target.committed)
        self.assertTrue(source.committed)
        self.assertTrue(
            any("analytics.dim_enterprise" in query for query in execute_calls)
        )
        refresh.assert_called_once()
        reconcile.assert_called_once()
        self.assertTrue(
            any(
                "UPDATE analytics.sync_state" in query
                for query in target.cursor_obj.executed
            )
        )

    def test_failed_reconciliation_rolls_back_without_advancing_watermark(self) -> None:
        source = FakeConnection(
            "source",
            [[(1, "Empresa", "SP", "Sao Paulo")], [], [], [], [], [], []],
        )
        target = FakeConnection("target")

        with patch.dict(
            os.environ,
            {
                "PRODUCTION_DATABASE_URL": "postgresql://source",
                "ANALYTICS_SYNC_DATABASE_URL": "postgresql://target",
            },
            clear=True,
        ), patch.object(
            sync_analytics.psycopg2,
            "connect",
            side_effect=[source, target],
        ), patch.object(
            sync_analytics,
            "refresh_derived_rows",
            return_value={},
        ), patch.object(
            sync_analytics,
            "reconcile_deleted_rows",
            side_effect=RuntimeError("falha no delete"),
        ):
            with self.assertRaisesRegex(RuntimeError, "falha no delete"):
                sync_analytics.main()

        self.assertTrue(target.rolled_back)
        self.assertFalse(target.committed)
        self.assertFalse(
            any(
                "UPDATE analytics.sync_state" in query
                for query in target.cursor_obj.executed
            )
        )


if __name__ == "__main__":
    unittest.main()
