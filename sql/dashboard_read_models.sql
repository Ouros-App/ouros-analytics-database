-- Read models consumed by ms-telemetry-dashboard-service.

CREATE OR REPLACE VIEW analytics.dashboard_farm_overview AS
SELECT
    farm_id,
    enterprise_id,
    farm_name,
    chickens_now,
    poultry_capacity
FROM analytics.dim_farm;

CREATE OR REPLACE VIEW analytics.dashboard_enterprise_summary AS
SELECT
    enterprise_id,
    COUNT(*)::bigint AS farm_count,
    COALESCE(SUM(chickens_now), 0)::bigint AS chickens_now,
    COALESCE(SUM(poultry_capacity), 0)::bigint AS poultry_capacity
FROM analytics.dim_farm
GROUP BY enterprise_id;

CREATE OR REPLACE VIEW analytics.dashboard_financial_summary AS
SELECT
    farm_id,
    enterprise_id,
    delivery_date,
    SUM(received_chickens)::bigint AS received_chickens,
    SUM(delivered_chickens)::bigint AS delivered_chickens,
    SUM(lost_chickens)::bigint AS lost_chickens,
    ROUND(SUM(cost), 2) AS cost
FROM analytics.fact_lot
GROUP BY farm_id, enterprise_id, delivery_date;

CREATE OR REPLACE VIEW analytics.dashboard_consumption_history AS
SELECT
    farm_id,
    enterprise_id,
    month_start,
    SUM(water_consumed_m3) AS water_consumed_m3,
    SUM(energy_consumed_kwh) AS energy_consumed_kwh,
    SUM(chickens_reference) AS chickens_reference
FROM analytics.fact_farm_consumption_monthly
GROUP BY farm_id, enterprise_id, month_start;

CREATE OR REPLACE VIEW analytics.dashboard_water_reading_history AS
SELECT
    farm_id,
    enterprise_id,
    registration_date,
    SUM(water_consumed_m3) AS water_consumed_m3
FROM analytics.fact_water_registry
GROUP BY farm_id, enterprise_id, registration_date;

CREATE OR REPLACE VIEW analytics.dashboard_goal_progress AS
SELECT
    g.farm_id,
    f.enterprise_id,
    g.goal_status,
    g.goal_type,
    COUNT(*)::bigint AS goal_count
FROM analytics.fact_goal AS g
LEFT JOIN analytics.dim_farm AS f ON f.farm_id = g.farm_id
GROUP BY g.farm_id, f.enterprise_id, g.goal_status, g.goal_type;

CREATE OR REPLACE VIEW analytics.dashboard_sync_status AS
SELECT
    sync_name AS dataset,
    last_sync AS last_success_at,
    NULL::text AS status
FROM analytics.sync_state;

GRANT SELECT ON
    analytics.dashboard_farm_overview,
    analytics.dashboard_enterprise_summary,
    analytics.dashboard_financial_summary,
    analytics.dashboard_consumption_history,
    analytics.dashboard_water_reading_history,
    analytics.dashboard_goal_progress,
    analytics.dashboard_sync_status
TO analytics_ro;
