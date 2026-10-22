"""Read-only PostgreSQL source-query contract for the MongoDB Atlas sync.

These SQL statements read the operational Neon database only. There is no
PostgreSQL analytics destination, migration, or writer in this repository.
The Mongo synchronizer may adapt legacy source columns through its explicit
production-schema compatibility layer.
"""

WATER_REGISTRY_QUERY = """
    SELECT w.id, f.id, f.id_enterprise, btrim(f.name), lower(btrim(f.region)),
           w.registration_date::date,
           (w.end_hydrometer - w.start_hydrometer)::numeric
    FROM public.water_registries w
    JOIN public.farms f ON f.id = w.id_farm
"""

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
            "farm_id", "enterprise_id", "enterprise_name", "farm_name", "region",
            "place", "area_property", "poultry_capacity", "chickens_now",
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
            "lot_id", "enterprise_id", "farm_id", "enterprise_name", "farm_name",
            "region", "delivery_date", "received_chickens", "delivered_chickens",
            "lost_chickens", "cost",
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
            SELECT w.id_farm AS farm_id, date_trunc('month', w.registration_date)::date AS month_start
            FROM public.water_registries w
            JOIN changed_farms c ON c.farm_id = w.id_farm
            UNION
            SELECT e.id_farm, date_trunc('month', e.registration_date)::date
            FROM public.energy_registries e
            JOIN changed_farms c ON c.farm_id = e.id_farm
        ), water_totals AS (
            SELECT w.id_farm AS farm_id,
                   date_trunc('month', w.registration_date)::date AS month_start,
                   SUM(w.end_hydrometer - w.start_hydrometer) AS water_consumed_m3
            FROM public.water_registries w
            JOIN changed_months c ON c.farm_id = w.id_farm
                AND c.month_start = date_trunc('month', w.registration_date)::date
            GROUP BY w.id_farm, date_trunc('month', w.registration_date)::date
        ), energy_totals AS (
            SELECT e.id_farm AS farm_id,
                   date_trunc('month', e.registration_date)::date AS month_start,
                   SUM(e.energy_consumption) AS energy_consumed_kwh
            FROM public.energy_registries e
            JOIN changed_months c ON c.farm_id = e.id_farm
                AND c.month_start = date_trunc('month', e.registration_date)::date
            GROUP BY e.id_farm, date_trunc('month', e.registration_date)::date
        )
        SELECT c.month_start, f.id, f.id_enterprise, btrim(f.name), lower(btrim(f.region)),
               f.chickens_now, COALESCE(w.water_consumed_m3, 0),
               COALESCE(e.energy_consumed_kwh, 0)
        FROM changed_months c
        JOIN public.farms f ON f.id = c.farm_id
        LEFT JOIN water_totals w ON w.farm_id = c.farm_id AND w.month_start = c.month_start
        LEFT JOIN energy_totals e ON e.farm_id = c.farm_id AND e.month_start = c.month_start
        """,
        (
            "month_start", "farm_id", "enterprise_id", "farm_name", "region",
            "chickens_reference", "water_consumed_m3", "energy_consumed_kwh",
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
            "payment_id", "enterprise_id", "plan_id", "payment_type", "amount",
            "payment_date", "plan_title", "plan_duration_days", "plan_price",
        ),
        ("payment_id",),
    ),
    (
        "fact_goal",
        """
        WITH bounds AS (SELECT %s::timestamptz AS last_sync, %s::timestamptz AS sync_end)
        SELECT g.id, 'individual', g.id_farm, lower(btrim(f.region)), lower(btrim(g.type)),
               lower(btrim(g.status)), btrim(g.title), g.target_value, NULL::timestamp, NULL::timestamp
        FROM public.individual_goals g
        JOIN public.farms f ON f.id = g.id_farm
        CROSS JOIN bounds b
        WHERE (g.updated_at > b.last_sync AND g.updated_at <= b.sync_end)
           OR (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
        UNION ALL
        SELECT g.id, 'state', g.id_farm, lower(btrim(f.region)), lower(btrim(g.type)),
               lower(btrim(g.status)), btrim(g.title), g.target_value,
               g.date_creation, g.date_end
        FROM public.state_goals g
        JOIN public.farms f ON f.id = g.id_farm
        CROSS JOIN bounds b
        WHERE (g.updated_at > b.last_sync AND g.updated_at <= b.sync_end)
           OR (f.updated_at > b.last_sync AND f.updated_at <= b.sync_end)
        """,
        (
            "goal_id", "goal_scope", "farm_id", "region", "goal_type", "goal_status",
            "goal_title", "target_value", "created_at", "end_at",
        ),
        ("goal_scope", "goal_id"),
    ),
]


TIP_FEEDBACK_SNAPSHOT_QUERY = """
    SELECT r.id, t.id, ft.id_farm, btrim(f.name), r.rating,
           COALESCE(
               array_agg(DISTINCT lower(btrim(c.category)) ORDER BY lower(btrim(c.category)))
                   FILTER (WHERE c.id IS NOT NULL),
               ARRAY[]::text[]
           )
    FROM public.reviews r
    JOIN public.tips t ON t.id = r.id_tip
    JOIN public.farms_tips ft ON ft.id_tip = t.id
    JOIN public.farms f ON f.id = ft.id_farm
    LEFT JOIN public.tip_categories tc ON tc.id_tip = t.id
    LEFT JOIN public.categories c ON c.id = tc.id_category
    GROUP BY r.id, t.id, ft.id_farm, f.name, r.rating
"""
TIP_FEEDBACK_COLUMNS = (
    "review_id", "tip_id", "farm_id", "farm_name", "rating", "categories",
)
TIP_FEEDBACK_CONFLICT = ("review_id", "farm_id")
