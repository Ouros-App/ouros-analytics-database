import unittest

from scripts.sync_analytics import STEPS
from scripts.sync_mongodb import mongo_compatible_query


class CurrentProductionSchemaCompatibilityTest(unittest.TestCase):
    def test_farm_fields_removed_from_production_are_nullable_in_poc(self):
        query = next(q for name, q, *_ in STEPS if name == "dim_farm")
        adapted = mongo_compatible_query("dim_farm", query)
        self.assertNotIn("f.place", adapted)
        self.assertNotIn("f.poultry_capacity", adapted)
        self.assertEqual(adapted.count("NULL::text"), 1)
        self.assertEqual(adapted.count("NULL::integer"), 1)

    def test_state_goals_follow_farm_links_and_per_farm_status(self):
        query = next(q for name, q, *_ in STEPS if name == "fact_goal")
        adapted = mongo_compatible_query("fact_goal", query)
        self.assertIn("JOIN public.farm_goals fg ON fg.id_goal = g.id", adapted)
        self.assertIn("JOIN public.farms f ON f.id = fg.id_farm", adapted)
        self.assertIn("SELECT g.id, 'state', fg.id_farm", adapted)
        self.assertIn("lower(btrim(fg.status))", adapted)
        self.assertNotIn("SELECT g.id, 'state', g.id_farm", adapted)
        self.assertIn("JOIN public.farms f ON f.id = g.id_farm", adapted)

    def test_unrelated_queries_keep_same_sql(self):
        for name, query, *_ in STEPS:
            if name not in {"dim_farm", "fact_goal"}:
                self.assertEqual(mongo_compatible_query(name, query), query)

    def test_goal_query_drift_fails_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy fact_goal query changed"):
            mongo_compatible_query("fact_goal", "SELECT 1")


if __name__ == "__main__":
    unittest.main()
