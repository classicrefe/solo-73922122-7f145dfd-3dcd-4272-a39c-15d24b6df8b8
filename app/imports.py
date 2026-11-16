"""Transactional batch imports with idempotent reuse and all-or-nothing conflicts.

Rules implemented here:
* Same key (payment_id+revision_id, or sample_id) with identical content ->
  the existing row is reused, the upload stays idempotent.
* Same key with different content -> the ENTIRE batch is rejected (the
  caller's transaction rolls back, nothing from the batch is stored).
* Every imported revision row is stamped with the batch_id it arrived in;
  a training run's revision scope is "all batches committed before the run
  was created", i.e. ``batch_id <= run.revision_watermark_batch``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection

from .csvio import RevisionRow, SampleRow

# Advisory lock key serializing "batch acceptance" (sample AND revision)
# against "training run creation" so a run's watermarks are exact
# acceptance instants: every batch committed before the lock is included,
# every batch after it is not. Because the lock is held for the whole
# import transaction, identity-generated batch ids / sample seqs are
# assigned in commit order and MAX(...) read under the same lock is exact.
ACCEPTANCE_LOCK_KEY = 730001


class ConflictError(Exception):
    """Same key already exists with different content; batch rejected."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        keys = ", ".join(c["key"] for c in conflicts[:5])
        super().__init__(
            f"{len(conflicts)} conflicting row(s) already exist with different "
            f"content; entire batch rejected. First keys: {keys}"
        )


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def import_revision_batch(
    conn: Connection, rows: list[RevisionRow], in_file_dups: int
) -> tuple[int, int, int]:
    """Insert one revision batch. Returns (batch_id, inserted, reused).

    Must be called inside a transaction; raises ConflictError to roll the
    whole batch back. Takes the acceptance advisory lock so training-run
    revision watermarks are exact: a batch is either entirely inside or
    entirely outside any run's scope, and a run created after this batch
    commits always includes it.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ACCEPTANCE_LOCK_KEY,))
    batch_id = conn.execute(
        """
        INSERT INTO revision_batches (rows_total, rows_inserted, rows_reused)
        VALUES (%s, 0, 0)
        RETURNING batch_id
        """,
        (len(rows) + in_file_dups,),
    ).fetchone()[0]
    inserted = 0
    reused = in_file_dups
    conflicts: list[dict[str, Any]] = []
    for r in rows:
        got = conn.execute(
            """
            INSERT INTO payment_revisions
                (payment_id, revision_id, account_id, occurred_at, visible_at,
                 amount_cents, revoked, batch_id)
            VALUES (%(payment_id)s, %(revision_id)s, %(account_id)s,
                    %(occurred_at)s, %(visible_at)s, %(amount_cents)s,
                    %(revoked)s, %(batch_id)s)
            ON CONFLICT (payment_id, revision_id) DO NOTHING
            RETURNING payment_id
            """,
            {
                "payment_id": r.payment_id,
                "revision_id": r.revision_id,
                "account_id": r.account_id,
                "occurred_at": r.occurred_at,
                "visible_at": r.visible_at,
                "amount_cents": r.amount_cents,
                "revoked": r.revoked,
                "batch_id": batch_id,
            },
        ).fetchone()
        if got:
            inserted += 1
            continue
        # Row already existed (same key). It keeps its ORIGINAL batch_id:
        # idempotent re-uploads never move a row into a newer batch, so a
        # re-upload cannot extend an old run's frozen scope.
        existing = conn.execute(
            """
            SELECT account_id, occurred_at, visible_at, amount_cents, revoked
            FROM payment_revisions
            WHERE payment_id = %s AND revision_id = %s
            """,
            (r.payment_id, r.revision_id),
        ).fetchone()
        if existing and tuple(existing) == r.content():
            reused += 1
        else:
            conflicts.append(
                {
                    "key": f"{r.payment_id}#{r.revision_id}",
                    "existing": {
                        "account_id": existing[0],
                        "occurred_at_utc": _ts(existing[1]),
                        "visible_at_utc": _ts(existing[2]),
                        "amount_cents": existing[3],
                        "revoked": existing[4],
                    }
                    if existing
                    else None,
                    "incoming": {
                        "account_id": r.account_id,
                        "occurred_at_utc": _ts(r.occurred_at),
                        "visible_at_utc": _ts(r.visible_at),
                        "amount_cents": r.amount_cents,
                        "revoked": r.revoked,
                    },
                }
            )
    if conflicts:
        raise ConflictError(conflicts)
    conn.execute(
        "UPDATE revision_batches SET rows_inserted = %s, rows_reused = %s "
        "WHERE batch_id = %s",
        (inserted, reused, batch_id),
    )
    return batch_id, inserted, reused


def import_sample_batch(
    conn: Connection, rows: list[SampleRow], in_file_dups: int
) -> tuple[int, int, int]:
    """Insert one sample batch. Returns (batch_id, inserted, reused).

    Takes the acceptance advisory lock so training-run watermarks are exact.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ACCEPTANCE_LOCK_KEY,))
    batch_id = conn.execute(
        """
        INSERT INTO sample_batches (rows_total, rows_inserted, rows_reused)
        VALUES (%s, 0, 0)
        RETURNING batch_id
        """,
        (len(rows) + in_file_dups,),
    ).fetchone()[0]
    inserted = 0
    reused = in_file_dups
    conflicts: list[dict[str, Any]] = []
    for r in rows:
        got = conn.execute(
            """
            INSERT INTO samples (batch_id, sample_id, account_id, prediction_at)
            VALUES (%(batch_id)s, %(sample_id)s, %(account_id)s, %(prediction_at)s)
            ON CONFLICT (sample_id) DO NOTHING
            RETURNING seq
            """,
            {
                "batch_id": batch_id,
                "sample_id": r.sample_id,
                "account_id": r.account_id,
                "prediction_at": r.prediction_at,
            },
        ).fetchone()
        if got:
            inserted += 1
            continue
        existing = conn.execute(
            "SELECT account_id, prediction_at FROM samples WHERE sample_id = %s",
            (r.sample_id,),
        ).fetchone()
        if existing and tuple(existing) == r.content():
            reused += 1
        else:
            conflicts.append(
                {
                    "key": r.sample_id,
                    "existing": {
                        "account_id": existing[0],
                        "prediction_at_utc": _ts(existing[1]),
                    }
                    if existing
                    else None,
                    "incoming": {
                        "account_id": r.account_id,
                        "prediction_at_utc": _ts(r.prediction_at),
                    },
                }
            )
    if conflicts:
        raise ConflictError(conflicts)
    conn.execute(
        "UPDATE sample_batches SET rows_inserted = %s, rows_reused = %s WHERE batch_id = %s",
        (inserted, reused, batch_id),
    )
    return batch_id, inserted, reused
