-- Existing installations used review_id alone as the primary key.
-- This no longer matches the normalized production model where one tip can be
-- associated with multiple farms through public.farms_tips.
--
-- Fresh installations already receive the composite key from analytics_schema.sql;
-- this migration keeps existing databases compatible.

ALTER TABLE analytics.fact_tip_feedback
    DROP CONSTRAINT IF EXISTS fact_tip_feedback_pkey;

ALTER TABLE analytics.fact_tip_feedback
    ADD CONSTRAINT fact_tip_feedback_pkey PRIMARY KEY (review_id, farm_id);
