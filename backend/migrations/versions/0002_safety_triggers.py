"""Database-level safety invariants.

These triggers make the most important rules hold even if application code is wrong or someone
edits rows by hand:
  * A research case can never be RECOMMENDED unless its latest risk decision exists and is not BLOCK.
  * A committee memo can never RECOMMEND against a BLOCK risk decision.
  * A case's original hypothesis is immutable.
  * Audit log rows are append-only.
  * Risk configs are immutable once written (only status may move ACTIVE -> SUPERSEDED).
Also enables TimescaleDB on candles when the extension is available (optional).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION meridian_enforce_risk_veto_on_case() RETURNS trigger AS $$
        DECLARE latest text;
        BEGIN
          IF NEW.state = 'RECOMMENDED' AND (OLD.state IS DISTINCT FROM 'RECOMMENDED') THEN
            SELECT outcome INTO latest FROM risk_decisions
              WHERE case_id = NEW.id ORDER BY created_at DESC, id DESC LIMIT 1;
            IF latest IS NULL THEN
              RAISE EXCEPTION 'RISK_VETO: case % has no risk decision; cannot be RECOMMENDED', NEW.id;
            END IF;
            IF latest = 'BLOCK' THEN
              RAISE EXCEPTION 'RISK_VETO: case % latest risk decision is BLOCK; cannot be RECOMMENDED', NEW.id;
            END IF;
          END IF;
          IF NEW.hypothesis IS DISTINCT FROM OLD.hypothesis THEN
            RAISE EXCEPTION 'IMMUTABLE: original hypothesis of case % cannot be modified', NEW.id;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_case_risk_veto BEFORE UPDATE ON research_cases
          FOR EACH ROW EXECUTE FUNCTION meridian_enforce_risk_veto_on_case();

        CREATE OR REPLACE FUNCTION meridian_enforce_risk_veto_on_memo() RETURNS trigger AS $$
        DECLARE o text;
        BEGIN
          SELECT outcome INTO o FROM risk_decisions WHERE id = NEW.risk_decision_id;
          IF NEW.recommendation = 'RECOMMEND' AND (o IS NULL OR o = 'BLOCK') THEN
            RAISE EXCEPTION 'RISK_VETO: memo cannot RECOMMEND against risk outcome %', coalesce(o, 'MISSING');
          END IF;
          IF o = 'BLOCK' AND NEW.recommendation <> 'BLOCKED' THEN
            RAISE EXCEPTION 'RISK_VETO: memo for a BLOCK risk decision must be BLOCKED';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_memo_risk_veto BEFORE INSERT OR UPDATE ON committee_memos
          FOR EACH ROW EXECUTE FUNCTION meridian_enforce_risk_veto_on_memo();

        CREATE OR REPLACE FUNCTION meridian_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'APPEND_ONLY: % rows cannot be modified or deleted', TG_TABLE_NAME;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON audit_log
          FOR EACH ROW EXECUTE FUNCTION meridian_append_only();
        CREATE TRIGGER trg_risk_decisions_append_only BEFORE UPDATE OR DELETE ON risk_decisions
          FOR EACH ROW EXECUTE FUNCTION meridian_append_only();

        CREATE OR REPLACE FUNCTION meridian_risk_config_immutable() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'APPEND_ONLY: risk configs cannot be deleted';
          END IF;
          IF NEW.config IS DISTINCT FROM OLD.config OR NEW.version <> OLD.version
             OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.reason IS DISTINCT FROM OLD.reason THEN
            RAISE EXCEPTION 'IMMUTABLE: risk config v% cannot be edited; create a new version', OLD.version;
          END IF;
          IF NOT (OLD.status = 'ACTIVE' AND NEW.status = 'SUPERSEDED') AND NEW.status <> OLD.status THEN
            RAISE EXCEPTION 'IMMUTABLE: invalid risk config status change % -> %', OLD.status, NEW.status;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_risk_config_immutable BEFORE UPDATE OR DELETE ON risk_configs
          FOR EACH ROW EXECUTE FUNCTION meridian_risk_config_immutable();

        CREATE UNIQUE INDEX uq_one_active_risk_config ON risk_configs ((status)) WHERE status = 'ACTIVE';
        CREATE INDEX ix_events_unpublished ON events (seq) WHERE published_at IS NULL;
        CREATE INDEX ix_candles_lookup ON candles (instrument_id, granularity, ts DESC);
        """
    )
    # Optional TimescaleDB. Plain PostgreSQL (e.g. Railway's default image) skips this cleanly.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
            CREATE EXTENSION IF NOT EXISTS timescaledb;
            PERFORM create_hypertable('candles', 'ts', migrate_data => true, if_not_exists => true);
          END IF;
        EXCEPTION WHEN OTHERS THEN
          RAISE NOTICE 'TimescaleDB unavailable, using plain table: %', SQLERRM;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_case_risk_veto ON research_cases;
        DROP TRIGGER IF EXISTS trg_memo_risk_veto ON committee_memos;
        DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_log;
        DROP TRIGGER IF EXISTS trg_risk_decisions_append_only ON risk_decisions;
        DROP TRIGGER IF EXISTS trg_risk_config_immutable ON risk_configs;
        DROP FUNCTION IF EXISTS meridian_enforce_risk_veto_on_case;
        DROP FUNCTION IF EXISTS meridian_enforce_risk_veto_on_memo;
        DROP FUNCTION IF EXISTS meridian_append_only;
        DROP FUNCTION IF EXISTS meridian_risk_config_immutable;
        DROP INDEX IF EXISTS uq_one_active_risk_config;
        DROP INDEX IF EXISTS ix_events_unpublished;
        DROP INDEX IF EXISTS ix_candles_lookup;
        """
    )
