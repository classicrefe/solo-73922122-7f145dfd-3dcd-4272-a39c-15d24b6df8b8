"""Database schema and connection pool.

Design constraint honored here: the service never holds a long-lived
snapshot transaction and never copies the source revision history for a
training run. A run's revision scope is a single integer — the highest
revision-batch id committed at run-creation time — stored on the run row
and applied as the predicate ``batch_id <= revision_watermark_batch`` in
the per-sample as-of query. All historical reconstruction is done per
sample, in short read transactions, against the live
``payment_revisions`` table.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS revision_batches (
    batch_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at   timestamptz NOT NULL DEFAULT now(),
    rows_total    integer NOT NULL,
    rows_inserted integer NOT NULL,
    rows_reused   integer NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_revisions (
    payment_id    text        NOT NULL,
    revision_id   integer     NOT NULL,
    account_id    text        NOT NULL,
    occurred_at   timestamptz NOT NULL,
    visible_at    timestamptz NOT NULL,
    amount_cents  bigint      NOT NULL,
    revoked       boolean     NOT NULL,
    batch_id      bigint      NOT NULL REFERENCES revision_batches(batch_id),
    imported_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (payment_id, revision_id)
);
CREATE INDEX IF NOT EXISTS ix_payment_revisions_visible
    ON payment_revisions (visible_at);
CREATE INDEX IF NOT EXISTS ix_payment_revisions_account_occurred
    ON payment_revisions (account_id, occurred_at);
-- ix_payment_revisions_batch is created in MIGRATION_SQL: on databases
-- upgraded from the pre-watermark schema the batch_id column does not
-- exist until the migration's ALTER TABLE runs.

CREATE TABLE IF NOT EXISTS sample_batches (
    batch_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    accepted_at   timestamptz NOT NULL DEFAULT now(),
    rows_total    integer NOT NULL,
    rows_inserted integer NOT NULL,
    rows_reused   integer NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id      bigint NOT NULL REFERENCES sample_batches(batch_id),
    sample_id     text NOT NULL UNIQUE,
    account_id    text NOT NULL,
    prediction_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS training_runs (
    run_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    watermark_seq  bigint NOT NULL,
    revision_watermark_batch bigint NOT NULL,
    total_samples  integer NOT NULL,
    done_samples   integer NOT NULL DEFAULT 0,
    started_at     timestamptz,
    finished_at    timestamptz,
    error          text
);

CREATE TABLE IF NOT EXISTS run_results (
    run_id         bigint NOT NULL REFERENCES training_runs(run_id),
    sample_seq     bigint NOT NULL REFERENCES samples(seq),
    sample_id      text NOT NULL,
    account_id     text NOT NULL,
    prediction_at  timestamptz NOT NULL,
    amount_cents   bigint NOT NULL,
    payment_count  integer NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, sample_seq)
);
"""

# Idempotent upgrade for databases created before revision watermarks
# existed. Fresh installs already have both columns (see SCHEMA_SQL), in
# which case every statement below is a no-op.
#
# Legacy revision rows (imported before the fix) have no batch; they are
# backfilled into one synthetic "legacy import" batch. Because that batch
# is created at migration time — before any new batch can be accepted —
# its id is below every future run's watermark, so legacy rows stay
# visible to all runs, exactly as they were before the upgrade.
MIGRATION_SQL = """
ALTER TABLE payment_revisions
    ADD COLUMN IF NOT EXISTS batch_id bigint
    REFERENCES revision_batches(batch_id);
ALTER TABLE training_runs
    ADD COLUMN IF NOT EXISTS revision_watermark_batch bigint;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'payment_revisions'::regclass
          AND attname = 'batch_id'
          AND NOT attnotnull
    ) THEN
        IF EXISTS (SELECT 1 FROM payment_revisions WHERE batch_id IS NULL) THEN
            INSERT INTO revision_batches (rows_total, rows_inserted, rows_reused)
            SELECT count(*), count(*), 0 FROM payment_revisions
            WHERE batch_id IS NULL;
            UPDATE payment_revisions
            SET batch_id = (SELECT max(batch_id) FROM revision_batches)
            WHERE batch_id IS NULL;
        END IF;
        ALTER TABLE payment_revisions ALTER COLUMN batch_id SET NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'training_runs'::regclass
          AND attname = 'revision_watermark_batch'
          AND NOT attnotnull
    ) THEN
        UPDATE training_runs
        SET revision_watermark_batch =
            (SELECT COALESCE(max(batch_id), 0) FROM revision_batches)
        WHERE revision_watermark_batch IS NULL;
        ALTER TABLE training_runs
            ALTER COLUMN revision_watermark_batch SET NOT NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_payment_revisions_batch
    ON payment_revisions (batch_id);
"""


def create_pool(database_url: str, max_size: int = 10) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=1, max_size=max_size, open=True)
    return pool


def init_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(MIGRATION_SQL)
