import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to run PostgreSQL integration tests",
)
class DashboardReadModelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        with cls.connection.cursor() as cursor:
            for filename in (
                "analytics_schema.sql",
                "sync_state.sql",
                "analytics_readonly_user.sql",
                "dashboard_read_models.sql",
            ):
                cursor.execute((ROOT / "sql" / filename).read_text(encoding="utf-8"))

            cursor.execute(
                "INSERT INTO analytics.dim_enterprise (enterprise_id, enterprise_name) "
                "VALUES (81001, 'Integration enterprise')"
            )
            cursor.execute(
                """
                INSERT INTO analytics.dim_farm (
                    farm_id, enterprise_id, enterprise_name, farm_name, region,
                    place, area_property, poultry_capacity, chickens_now
                ) VALUES (71001, 81001, 'Integration enterprise', 'Integration farm',
                          'sp', 'south', 1, 1000, 800)
                """
            )
            cursor.execute(
                """
                INSERT INTO analytics.fact_farm_consumption_monthly (
                    month_start, farm_id, enterprise_id, farm_name, region,
                    chickens_reference, water_consumed_m3, energy_consumed_kwh
                ) VALUES ('2026-09-01', 71001, 81001, 'Integration farm', 'sp',
                          800, 14.25, 31.5)
                """
            )
            cursor.execute(
                "INSERT INTO analytics.sync_state (sync_name, last_sync) "
                "VALUES ('integration', '2026-10-12T12:00:00+00')"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.rollback()
        cls.connection.close()

    def test_telemetry_views_work_with_reader_role_and_sync_watermark(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("SET ROLE analytics_ro")
            cursor.execute(
                """
                SELECT month_start, water_consumed_m3, energy_consumed_kwh,
                       chickens_reference
                FROM analytics.dashboard_consumption_history
                WHERE farm_id = 71001 AND enterprise_id = 81001
                """
            )
            self.assertEqual(
                cursor.fetchone(),
                (datetime(2026, 9, 1).date(), 14.25, 31.5, 800),
            )
            cursor.execute(
                """
                SELECT MIN(last_success_at),
                       COALESCE(
                           BOOL_OR(last_success_at IS NULL OR status IN ('error', 'failed')),
                           FALSE
                       )
                FROM analytics.dashboard_sync_status
                """
            )
            last_success_at, has_error = cursor.fetchone()
            self.assertEqual(
                last_success_at,
                datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
            )
            self.assertFalse(has_error)
