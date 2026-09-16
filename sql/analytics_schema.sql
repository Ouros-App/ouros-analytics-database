CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.dim_enterprise (
    enterprise_id INTEGER PRIMARY KEY,
    enterprise_name TEXT NOT NULL,
    state CHAR(2),
    city TEXT
);

CREATE TABLE IF NOT EXISTS analytics.dim_farm (
    farm_id INTEGER PRIMARY KEY,
    enterprise_id INTEGER NOT NULL REFERENCES analytics.dim_enterprise(enterprise_id),
    enterprise_name TEXT NOT NULL,
    farm_name TEXT NOT NULL,
    region TEXT NOT NULL,
    place TEXT NOT NULL,
    area_property NUMERIC(12,2) NOT NULL CHECK (area_property > 0),
    poultry_capacity INTEGER NOT NULL CHECK (poultry_capacity >= 0),
    chickens_now INTEGER NOT NULL CHECK (chickens_now >= 0),
    capacity_utilization_pct NUMERIC(7,2)
        GENERATED ALWAYS AS (
            ROUND(chickens_now * 100.0 / NULLIF(poultry_capacity, 0), 2)
        ) STORED,
    available_capacity INTEGER
        GENERATED ALWAYS AS (GREATEST(poultry_capacity - chickens_now, 0)) STORED
);

CREATE TABLE IF NOT EXISTS analytics.fact_lot (
    lot_id INTEGER PRIMARY KEY,
    enterprise_id INTEGER NOT NULL REFERENCES analytics.dim_enterprise(enterprise_id),
    farm_id INTEGER NOT NULL REFERENCES analytics.dim_farm(farm_id),
    enterprise_name TEXT NOT NULL,
    farm_name TEXT NOT NULL,
    region TEXT NOT NULL,
    delivery_date DATE NOT NULL,
    received_chickens INTEGER NOT NULL CHECK (received_chickens >= 0),
    delivered_chickens INTEGER NOT NULL CHECK (delivered_chickens >= 0),
    lost_chickens INTEGER NOT NULL CHECK (lost_chickens >= 0),
    cost NUMERIC(14,2) NOT NULL CHECK (cost >= 0),
    alive_chickens INTEGER
        GENERATED ALWAYS AS (received_chickens - delivered_chickens - lost_chickens) STORED,
    delivery_rate_pct NUMERIC(7,2)
        GENERATED ALWAYS AS (
            ROUND(delivered_chickens * 100.0 / NULLIF(received_chickens, 0), 2)
        ) STORED,
    mortality_rate_pct NUMERIC(7,2)
        GENERATED ALWAYS AS (
            ROUND(lost_chickens * 100.0 / NULLIF(received_chickens, 0), 2)
        ) STORED,
    cost_per_delivered_chicken NUMERIC(14,2)
        GENERATED ALWAYS AS (ROUND(cost / NULLIF(delivered_chickens, 0), 2)) STORED,
    CHECK (delivered_chickens + lost_chickens <= received_chickens)
);

CREATE TABLE IF NOT EXISTS analytics.fact_farm_consumption_monthly (
    month_start DATE NOT NULL CHECK (month_start = date_trunc('month', month_start)::DATE),
    farm_id INTEGER NOT NULL REFERENCES analytics.dim_farm(farm_id),
    enterprise_id INTEGER NOT NULL REFERENCES analytics.dim_enterprise(enterprise_id),
    farm_name TEXT NOT NULL,
    region TEXT NOT NULL,
    chickens_reference INTEGER NOT NULL CHECK (chickens_reference >= 0),
    water_consumed_m3 NUMERIC(14,3) NOT NULL CHECK (water_consumed_m3 >= 0),
    energy_consumed_kwh NUMERIC(14,3) NOT NULL CHECK (energy_consumed_kwh >= 0),
    water_m3_per_chicken NUMERIC(14,4)
        GENERATED ALWAYS AS (
            ROUND(water_consumed_m3 / NULLIF(chickens_reference, 0), 4)
        ) STORED,
    energy_kwh_per_chicken NUMERIC(14,4)
        GENERATED ALWAYS AS (
            ROUND(energy_consumed_kwh / NULLIF(chickens_reference, 0), 4)
        ) STORED,
    cgi_score NUMERIC(14,2)
        GENERATED ALWAYS AS (
            ROUND(
                water_consumed_m3 / NULLIF(chickens_reference, 0) * 0.7
                + energy_consumed_kwh / NULLIF(chickens_reference, 0) * 0.3,
                2
            )
        ) STORED,
    PRIMARY KEY (month_start, farm_id)
);

CREATE TABLE IF NOT EXISTS analytics.fact_payment (
    payment_id INTEGER PRIMARY KEY,
    enterprise_id INTEGER NOT NULL REFERENCES analytics.dim_enterprise(enterprise_id),
    plan_id INTEGER,
    payment_type TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    payment_date TIMESTAMP NOT NULL,
    plan_title TEXT,
    plan_duration_days INTEGER CHECK (plan_duration_days > 0),
    plan_price NUMERIC(12,2) CHECK (plan_price > 0)
);

CREATE TABLE IF NOT EXISTS analytics.fact_goal (
    goal_id INTEGER NOT NULL,
    goal_scope TEXT NOT NULL CHECK (goal_scope IN ('individual', 'state')),
    farm_id INTEGER REFERENCES analytics.dim_farm(farm_id),
    region TEXT,
    goal_type TEXT NOT NULL,
    goal_status TEXT NOT NULL,
    goal_title TEXT NOT NULL,
    target_value NUMERIC NOT NULL CHECK (target_value > 0),
    created_at TIMESTAMP,
    end_at TIMESTAMP,
    PRIMARY KEY (goal_scope, goal_id),
    CHECK (end_at IS NULL OR created_at IS NULL OR end_at >= created_at)
);

CREATE TABLE IF NOT EXISTS analytics.fact_tip_feedback (
    review_id INTEGER PRIMARY KEY,
    tip_id INTEGER NOT NULL,
    farm_id INTEGER NOT NULL REFERENCES analytics.dim_farm(farm_id),
    farm_name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 0 AND 5),
    categories TEXT[] NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS fact_lot_farm_date_idx
    ON analytics.fact_lot (farm_id, delivery_date DESC);
CREATE INDEX IF NOT EXISTS fact_lot_enterprise_date_idx
    ON analytics.fact_lot (enterprise_id, delivery_date DESC);
CREATE INDEX IF NOT EXISTS fact_consumption_farm_month_idx
    ON analytics.fact_farm_consumption_monthly (farm_id, month_start DESC);
CREATE INDEX IF NOT EXISTS fact_consumption_region_month_idx
    ON analytics.fact_farm_consumption_monthly (region, month_start DESC);
CREATE INDEX IF NOT EXISTS fact_payment_enterprise_date_idx
    ON analytics.fact_payment (enterprise_id, payment_date DESC);
CREATE INDEX IF NOT EXISTS fact_goal_status_idx
    ON analytics.fact_goal (goal_status, end_at);
CREATE INDEX IF NOT EXISTS fact_feedback_farm_idx
    ON analytics.fact_tip_feedback (farm_id);

CREATE OR REPLACE VIEW analytics.v_farm_dashboard AS
WITH lot_metrics AS (
    SELECT
        farm_id,
        SUM(received_chickens) AS received_chickens,
        SUM(delivered_chickens) AS delivered_chickens,
        SUM(lost_chickens) AS lost_chickens,
        SUM(cost) AS lot_cost,
        ROUND(AVG(mortality_rate_pct), 2) AS average_mortality_rate_pct
    FROM analytics.fact_lot
    GROUP BY farm_id
), latest_consumption AS (
    SELECT DISTINCT ON (farm_id)
        farm_id,
        month_start,
        water_consumed_m3,
        energy_consumed_kwh,
        water_m3_per_chicken,
        energy_kwh_per_chicken,
        cgi_score
    FROM analytics.fact_farm_consumption_monthly
    ORDER BY farm_id, month_start DESC
)
SELECT
    f.farm_id,
    f.enterprise_id,
    f.enterprise_name,
    f.farm_name,
    f.region,
    f.place,
    f.area_property,
    f.poultry_capacity,
    f.chickens_now,
    f.capacity_utilization_pct,
    f.available_capacity,
    COALESCE(l.received_chickens, 0) AS received_chickens,
    COALESCE(l.delivered_chickens, 0) AS delivered_chickens,
    COALESCE(l.lost_chickens, 0) AS lost_chickens,
    COALESCE(l.lot_cost, 0) AS lot_cost,
    l.average_mortality_rate_pct,
    c.month_start AS latest_consumption_month,
    c.water_consumed_m3,
    c.energy_consumed_kwh,
    c.water_m3_per_chicken,
    c.energy_kwh_per_chicken,
    c.cgi_score
FROM analytics.dim_farm f
LEFT JOIN lot_metrics l ON l.farm_id = f.farm_id
LEFT JOIN latest_consumption c ON c.farm_id = f.farm_id;
