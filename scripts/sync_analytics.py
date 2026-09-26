#!/usr/bin/env python3
import os

import psycopg2
from psycopg2.extras import execute_values


SYNC_NAME = "production_to_analytics"


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variavel de ambiente obrigatoria ausente: {name}")
    return value


def upsert(
    cur,
    table: str,
    columns: tuple[str, ...],
    conflict: tuple[str, ...],
    rows: list[tuple],
) -> int:
    if not rows:
        return 0
    names = ", ".join(columns)
    keys = ", ".join(conflict)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in columns
        if column not in conflict
    )
    query = (
        f"INSERT INTO {table} ({names}) VALUES %s "
        f"ON CONFLICT ({keys}) DO UPDATE SET {updates}"
    )
    execute_values(cur, query, rows, page_size=1000)
    return len(rows)


def delete_rows(
    cur,
    table: str,
    key_columns: tuple[str, ...],
    keys: list[tuple],
) -> int:
    if not keys:
        return 0
    alias_columns = ", ".join(key_columns)
    predicate = " AND ".join(
        f"target.{column} = stale.{column}" for column in key_columns
    )
    query = (
        f"DELETE FROM {table} AS target "
        f"USING (VALUES %s) AS stale ({alias_columns}) "
        f"WHERE {predicate}"
    )
    execute_values(cur, query, keys, page_size=1000)
    return len(keys)


STEPS = [
    (
        "dim_enterprise",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT e.id, btrim(e.name), upper(NULLIF(btrim(a.state), ''))::char(2),
               NULLIF(btrim(a.city), '')
        FROM public.enterprises e
        LEFT JOIN public.addresses a ON a.id = e.id_address
        CROSS JOIN bounds b
        WHERE (e.updated_at > b.last_sync AND e.updated_at <= b.sync_end)
           OR (a.updated_at > b.last_sync AND a.updated_at <= b.sync_end)
        """,
        ("enterprise_id", "enterprise_name", "state", "city"),
        ("enterprise_id",),
    ),
    (
        "dim_farm",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT f.id, f.id_enterprise, btrim(e.name), btrim(f.name),
               lower(btrim(f.region)), btrim(f.place), f.area_property::numeric,
               f.poultry_capacity, f.chickens_now
        FROM public.farms f
        JOIN public.enterprises e ON e.id = f.id_enterprise
        CROSS JOIN bounds b
        WHERE (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
           OR (e.updated_at > b.last_sync AND e.updated_at <= b.sync_end)
        """,
        (
            "farm_id",
            "enterprise_id",
            "enterprise_name",
            "farm_name",
            "region",
            "place",
            "area_property",
            "poultry_capacity",
            "chickens_now",
        ),
        ("farm_id",),
    ),
    (
        "fact_lot",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT l.id, l.id_enterprise, l.id_farm, btrim(e.name), btrim(f.name),
               lower(btrim(f.region)), l.delivery_date, l.received_chickens,
               l.delivered_chickens, l.losts, l.cost::numeric
        FROM public.lots l
        JOIN public.enterprises e ON e.id = l.id_enterprise
        JOIN public.farms f ON f.id = l.id_farm
        CROSS JOIN bounds b
        WHERE (l.updated_at > b.last_sync AND l.updated_at <= b.sync_end)
           OR (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
           OR (e.updated_at > b.last_sync AND e.updated_at <= b.sync_end)
        """,
        (
            "lot_id",
            "enterprise_id",
            "farm_id",
            "enterprise_name",
            "farm_name",
            "region",
            "delivery_date",
            "received_chickens",
            "delivered_chickens",
            "lost_chickens",
            "cost",
        ),
        ("lot_id",),
    ),
    (
        "fact_farm_consumption_monthly",
        """
        WITH bounds AS (
            SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end
        ), changed_farms AS (
            SELECT f.id AS farm_id
            FROM public.farms f, bounds b
            WHERE f.updated_at > b.last_sync AND f.updated_at <= b.sync_end
            UNION
            SELECT w.id_farm
            FROM public.water_registries w, bounds b
            WHERE w.updated_at > b.last_sync AND w.updated_at <= b.sync_end
            UNION
            SELECT e.id_farm
            FROM public.energy_registries e, bounds b
            WHERE e.updated_at > b.last_sync AND e.updated_at <= b.sync_end
        ), changed_months AS (
            SELECT w.id_farm AS farm_id,
                   date_trunc('month', w.registration_date)::date AS month_start
            FROM public.water_registries w
            JOIN changed_farms c ON c.farm_id = w.id_farm
            UNION
            SELECT e.id_farm,
                   date_trunc('month', e.registration_date)::date
            FROM public.energy_registries e
            JOIN changed_farms c ON c.farm_id = e.id_farm
        ), water_totals AS (
            SELECT w.id_farm AS farm_id,
                   date_trunc('month', w.registration_date)::date AS month_start,
                   SUM(w.end_hydrometer - w.start_hydrometer) AS water_consumed_m3
            FROM public.water_registries w
            JOIN changed_months c
              ON c.farm_id = w.id_farm
             AND c.month_start = date_trunc('month', w.registration_date)::date
            GROUP BY w.id_farm, date_trunc('month', w.registration_date)::date
        ), energy_totals AS (
            SELECT e.id_farm AS farm_id,
                   date_trunc('month', e.registration_date)::date AS month_start,
                   SUM(e.energy_consumption) AS energy_consumed_kwh
            FROM public.energy_registries e
            JOIN changed_months c
              ON c.farm_id = e.id_farm
             AND c.month_start = date_trunc('month', e.registration_date)::date
            GROUP BY e.id_farm, date_trunc('month', e.registration_date)::date
        )
        SELECT c.month_start, f.id, f.id_enterprise, btrim(f.name),
               lower(btrim(f.region)), f.chickens_now,
               COALESCE(w.water_consumed_m3, 0),
               COALESCE(e.energy_consumed_kwh, 0)
        FROM changed_months c
        JOIN public.farms f ON f.id = c.farm_id
        LEFT JOIN water_totals w
          ON w.farm_id = c.farm_id
         AND w.month_start = c.month_start
        LEFT JOIN energy_totals e
          ON e.farm_id = c.farm_id
         AND e.month_start = c.month_start
        """,
        (
            "month_start",
            "farm_id",
            "enterprise_id",
            "farm_name",
            "region",
            "chickens_reference",
            "water_consumed_m3",
            "energy_consumed_kwh",
        ),
        ("month_start", "farm_id"),
    ),
    (
        "fact_payment",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT p.id, p.id_enterprise, ep.id_plan, lower(btrim(p.type)), p.value::numeric,
               p.date_creation, btrim(pl.title), pl.duration_days, pl.price::numeric
        FROM public.payments p
        JOIN public.enterprise_plans ep
          ON ep.id = p.id_enterprise_plan AND ep.id_enterprise = p.id_enterprise
        JOIN public.plans pl ON pl.id = ep.id_plan
        CROSS JOIN bounds b
        WHERE (p.updated_at > b.last_sync AND p.updated_at <= b.sync_end)
           OR (ep.updated_at > b.last_sync AND ep.updated_at <= b.sync_end)
           OR (pl.updated_at > b.last_sync AND pl.updated_at <= b.sync_end)
        """,
        (
            "payment_id",
            "enterprise_id",
            "plan_id",
            "payment_type",
            "amount",
            "payment_date",
            "plan_title",
            "plan_duration_days",
            "plan_price",
        ),
        ("payment_id",),
    ),
    (
        "fact_goal",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT g.id, 'individual', g.id_farm, lower(btrim(f.region)),
               lower(btrim(g.type)), lower(btrim(g.status)), btrim(g.title),
               g.target_value, NULL::timestamp, NULL::timestamp
        FROM public.individual_goals g
        JOIN public.farms f ON f.id = g.id_farm
        CROSS JOIN bounds b
        WHERE (g.updated_at > b.last_sync AND g.updated_at <= b.sync_end)
           OR (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
        UNION ALL
        SELECT g.id, 'state', g.id_farm, lower(btrim(f.region)),
               lower(btrim(g.type)), lower(btrim(g.status)), btrim(g.title),
               g.target_value, g.date_creation, g.date_end
        FROM public.state_goals g
        JOIN public.farms f ON f.id = g.id_farm
        CROSS JOIN bounds b
        WHERE (g.updated_at > b.last_sync AND g.updated_at <= b.sync_end)
           OR (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
        """,
        (
            "goal_id",
            "goal_scope",
            "farm_id",
            "region",
            "goal_type",
            "goal_status",
            "goal_title",
            "target_value",
            "created_at",
            "end_at",
        ),
        ("goal_scope", "goal_id"),
    ),
    (
        "fact_tip_feedback",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT r.id, t.id, t.id_farm, btrim(f.name), r.rating,
               COALESCE(
                   array_agg(
                       DISTINCT lower(btrim(c.category))
                       ORDER BY lower(btrim(c.category))
                   ) FILTER (WHERE c.id IS NOT NULL),
                   ARRAY[]::text[]
               )
        FROM public.reviews r
        JOIN public.tips t ON t.id = r.id_tip
        JOIN public.farms f ON f.id = t.id_farm
        LEFT JOIN public.tip_categories tc ON tc.id_tip = t.id
        LEFT JOIN public.categories c ON c.id = tc.id_category
        CROSS JOIN bounds b
        WHERE (r.updated_at > b.last_sync AND r.updated_at <= b.sync_end)
           OR (t.updated_at > b.last_sync AND t.updated_at <= b.sync_end)
           OR (tc.updated_at > b.last_sync AND tc.updated_at <= b.sync_end)
           OR (c.updated_at > b.last_sync AND c.updated_at <= b.sync_end)
        GROUP BY r.id, t.id, t.id_farm, f.name, r.rating
        """,
        ("review_id", "tip_id", "farm_id", "farm_name", "rating", "categories"),
        ("review_id",),
    ),
]


REFRESH_STEPS = [
    (
        "fact_farm_consumption_monthly",
        """
        WITH months AS (
            SELECT w.id_farm AS farm_id,
                   date_trunc('month', w.registration_date)::date AS month_start
            FROM public.water_registries w
            UNION
            SELECT e.id_farm,
                   date_trunc('month', e.registration_date)::date
            FROM public.energy_registries e
        ), water_totals AS (
            SELECT w.id_farm AS farm_id,
                   date_trunc('month', w.registration_date)::date AS month_start,
                   SUM(w.end_hydrometer - w.start_hydrometer) AS water_consumed_m3
            FROM public.water_registries w
            GROUP BY w.id_farm, date_trunc('month', w.registration_date)::date
        ), energy_totals AS (
            SELECT e.id_farm AS farm_id,
                   date_trunc('month', e.registration_date)::date AS month_start,
                   SUM(e.energy_consumption) AS energy_consumed_kwh
            FROM public.energy_registries e
            GROUP BY e.id_farm, date_trunc('month', e.registration_date)::date
        )
        SELECT m.month_start, f.id, f.id_enterprise, btrim(f.name),
               lower(btrim(f.region)), f.chickens_now,
               COALESCE(w.water_consumed_m3, 0),
               COALESCE(e.energy_consumed_kwh, 0)
        FROM months m
        JOIN public.farms f ON f.id = m.farm_id
        LEFT JOIN water_totals w
          ON w.farm_id = m.farm_id
         AND w.month_start = m.month_start
        LEFT JOIN energy_totals e
          ON e.farm_id = m.farm_id
         AND e.month_start = m.month_start
        """,
        (
            "month_start",
            "farm_id",
            "enterprise_id",
            "farm_name",
            "region",
            "chickens_reference",
            "water_consumed_m3",
            "energy_consumed_kwh",
        ),
        ("month_start", "farm_id"),
    ),
    (
        "fact_tip_feedback",
        """
        SELECT r.id, t.id, t.id_farm, btrim(f.name), r.rating,
               COALESCE(
                   array_agg(
                       DISTINCT lower(btrim(c.category))
                       ORDER BY lower(btrim(c.category))
                   ) FILTER (WHERE c.id IS NOT NULL),
                   ARRAY[]::text[]
               )
        FROM public.reviews r
        JOIN public.tips t ON t.id = r.id_tip
        JOIN public.farms f ON f.id = t.id_farm
        LEFT JOIN public.tip_categories tc ON tc.id_tip = t.id
        LEFT JOIN public.categories c ON c.id = tc.id_category
        GROUP BY r.id, t.id, t.id_farm, f.name, r.rating
        """,
        ("review_id", "tip_id", "farm_id", "farm_name", "rating", "categories"),
        ("review_id",),
    ),
]


RECONCILIATIONS = [
    (
        "fact_tip_feedback",
        ("review_id",),
        """
        SELECT r.id
        FROM public.reviews r
        JOIN public.tips t ON t.id = r.id_tip
        JOIN public.farms f ON f.id = t.id_farm
        """,
    ),
    (
        "fact_goal",
        ("goal_scope", "goal_id"),
        """
        SELECT 'individual', g.id
        FROM public.individual_goals g
        JOIN public.farms f ON f.id = g.id_farm
        UNION ALL
        SELECT 'state', g.id
        FROM public.state_goals g
        JOIN public.farms f ON f.id = g.id_farm
        """,
    ),
    (
        "fact_payment",
        ("payment_id",),
        """
        SELECT p.id
        FROM public.payments p
        JOIN public.enterprise_plans ep
          ON ep.id = p.id_enterprise_plan AND ep.id_enterprise = p.id_enterprise
        JOIN public.plans pl ON pl.id = ep.id_plan
        """,
    ),
    (
        "fact_farm_consumption_monthly",
        ("month_start", "farm_id"),
        """
        SELECT DISTINCT
               date_trunc('month', x.registration_date)::date AS month_start,
               x.id_farm
        FROM (
            SELECT w.id_farm, w.registration_date
            FROM public.water_registries w
            UNION ALL
            SELECT e.id_farm, e.registration_date
            FROM public.energy_registries e
        ) x
        JOIN public.farms f ON f.id = x.id_farm
        """,
    ),
    (
        "fact_lot",
        ("lot_id",),
        """
        SELECT l.id
        FROM public.lots l
        JOIN public.enterprises e ON e.id = l.id_enterprise
        JOIN public.farms f ON f.id = l.id_farm
        """,
    ),
    (
        "dim_farm",
        ("farm_id",),
        """
        SELECT f.id
        FROM public.farms f
        JOIN public.enterprises e ON e.id = f.id_enterprise
        """,
    ),
    (
        "dim_enterprise",
        ("enterprise_id",),
        """
        SELECT e.id
        FROM public.enterprises e
        """,
    ),
]


def refresh_derived_rows(source_cur, target_cur) -> dict[str, int]:
    counts = {}
    for name, query, columns, conflict in REFRESH_STEPS:
        source_cur.execute(query)
        rows = source_cur.fetchall()
        counts[f"refreshed_{name}"] = upsert(
            target_cur,
            f"analytics.{name}",
            columns,
            conflict,
            rows,
        )
    return counts


def reconcile_deleted_rows(source_cur, target_cur) -> dict[str, int]:
    counts = {}
    for table, key_columns, source_query in RECONCILIATIONS:
        source_cur.execute(source_query)
        source_keys = {tuple(row) for row in source_cur.fetchall()}

        columns = ", ".join(key_columns)
        target_cur.execute(f"SELECT {columns} FROM analytics.{table}")
        target_keys = {tuple(row) for row in target_cur.fetchall()}

        stale_keys = list(target_keys - source_keys)
        counts[f"deleted_{table}"] = delete_rows(
            target_cur,
            f"analytics.{table}",
            key_columns,
            stale_keys,
        )
    return counts


def main() -> None:
    source = psycopg2.connect(env("PRODUCTION_DATABASE_URL"))
    target = psycopg2.connect(env("ANALYTICS_SYNC_DATABASE_URL"))
    counts = {}
    sync_end = None
    try:
        source.set_session(readonly=True, autocommit=False)
        with target:
            with target.cursor() as target_cur, source.cursor() as source_cur:
                target_cur.execute(
                    "INSERT INTO analytics.sync_state (sync_name) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (SYNC_NAME,),
                )
                target_cur.execute(
                    "SELECT last_sync FROM analytics.sync_state "
                    "WHERE sync_name = %s FOR UPDATE",
                    (SYNC_NAME,),
                )
                last_sync = target_cur.fetchone()[0]

                source_cur.execute("SELECT CURRENT_TIMESTAMP")
                sync_end = source_cur.fetchone()[0]

                for name, query, columns, conflict in STEPS:
                    source_cur.execute(query, (last_sync, sync_end))
                    counts[name] = upsert(
                        target_cur,
                        f"analytics.{name}",
                        columns,
                        conflict,
                        source_cur.fetchall(),
                    )

                counts.update(refresh_derived_rows(source_cur, target_cur))
                counts.update(reconcile_deleted_rows(source_cur, target_cur))

                target_cur.execute(
                    "UPDATE analytics.sync_state "
                    "SET last_sync = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE sync_name = %s",
                    (sync_end, SYNC_NAME),
                )
        source.commit()
    finally:
        source.close()
        target.close()

    print(f"Sincronizacao concluida ate {sync_end.isoformat()}: {counts}")


if __name__ == "__main__":
    main()
