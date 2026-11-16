#!/usr/bin/env bash
# Example 2 — concurrent uploads while a run is in progress: sample
# watermark, REVISION watermark (the fixed defect), idempotent re-upload,
# and all-or-nothing conflict rejection.
#
# A run freezes TWO acceptance watermarks at creation: the sample batches
# and the payment-revision batches committed so far. Batches uploaded
# while it runs are accepted (201) but belong to the NEXT run — including
# "backdated" revisions whose visible_at lies before the samples'
# prediction time. Before this fix, such mid-run backfills changed the
# amounts of not-yet-computed samples, so identical samples inside one
# run disagreed.
#
# Expected: all 5000 ex2-* samples of run 1 amount to 100 (base revision
# only), although a past-visible correction (250) and a past-visible new
# payment (5) were uploaded mid-run. Run 2 then shows 255 everywhere.
#
# Tip: to widen the observable window, start the stack with a per-chunk
# delay, e.g.  RUN_CHUNK_DELAY_MS=50 docker compose up -d --build
set -euo pipefail
cd "$(dirname "$0")"
source ./_common.sh
require_app

DATA=/tmp/example2_samples.csv
python3 - <<'EOF'
import csv
with open('/tmp/example2_samples.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['sample_id', 'account_id', 'prediction_at_utc'])
    for i in range(5000):
        w.writerow([f'ex2-{i:05d}', 'acct-EX2', '2026-11-15T12:00:00Z'])
print('generated 5000 samples -> /tmp/example2_samples.csv')
EOF

echo "== 0. import base revision for acct-EX2 (ex2-p1 = 100, visible before T) =="
post_csv /payment-revision-batches data/revisions_ex2_base.csv; echo

echo "== 1. upload 5000 samples and create a run =="
post_csv /sample-batches "$DATA"; echo
RUN1_JSON=$(curl -sf -X POST "$BASE/training-runs")
RUN1=$(json_field "$RUN1_JSON" "d['run_id']")
TOTAL1=$(json_field "$RUN1_JSON" "d['total_samples']")
echo "run $RUN1 created: total_samples=$TOTAL1 revision_watermark_batch=$(json_field "$RUN1_JSON" "d['revision_watermark_batch']")"

echo "== 2. while it runs: upload 50 more samples (accepted, but excluded from run $RUN1) =="
python3 - <<'EOF'
import csv
with open('/tmp/example2_extra.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['sample_id', 'account_id', 'prediction_at_utc'])
    for i in range(50):
        w.writerow([f'ex2-extra-{i:03d}', 'acct-EX2', '2026-11-15T12:00:00Z'])
EOF
post_csv /sample-batches /tmp/example2_extra.csv; echo

echo "== 3. while it runs: BACKDATED correction ex2-p1#2 (250) + new payment ex2-p2 (5),"
echo "      both visible BEFORE the samples' prediction time -> accepted (201),"
echo "      but run $RUN1 must not see them =="
post_csv /payment-revision-batches data/revisions_ex2_backdated.csv; echo

echo "== 4. idempotent re-upload of identical revisions (all reused) =="
post_csv /payment-revision-batches data/revisions_base.csv; echo

echo "== 5. conflicting revision (p1#1 with different amount) -> HTTP 409, whole batch rejected =="
post_csv_expect_conflict /payment-revision-batches data/revisions_conflict.csv; echo
echo "revision rows still stored: $(curl -sf "$BASE/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['payment_revisions'])")"

echo "== 6. run $RUN1 progress (total stays $TOTAL1) =="
wait_run "$RUN1"
ROWS=$(curl -sf "$BASE/training-runs/$RUN1/dataset.csv" | tail -n +2 | wc -l)
EXTRA=$(curl -sf "$BASE/training-runs/$RUN1/dataset.csv" | grep -c '^ex2-extra-' || true)
echo "run $RUN1 dataset rows: $ROWS (total_samples=$TOTAL1), ex2-extra rows: $EXTRA (must be 0)"

echo "== 7. the fixed defect: identical samples, ONE run, ONE amount =="
AMOUNTS=$(curl -sf "$BASE/training-runs/$RUN1/dataset.csv" | grep '^ex2-' | cut -d, -f4 | sort -u)
echo "distinct amounts among ex2-* rows of run $RUN1: $(echo $AMOUNTS)"
[ "$AMOUNTS" = "100" ] && echo "OK: mid-run backfill did NOT leak into run $RUN1 (all 100)"

echo "== 8. next run picks up the late sample batch AND the backdated revisions =="
RUN2_JSON=$(curl -sf -X POST "$BASE/training-runs")
RUN2=$(json_field "$RUN2_JSON" "d['run_id']")
echo "run $RUN2 created: revision_watermark_batch=$(json_field "$RUN2_JSON" "d['revision_watermark_batch']")"
wait_run "$RUN2"
EXTRA2=$(curl -sf "$BASE/training-runs/$RUN2/dataset.csv" | grep -c '^ex2-extra-' || true)
AMOUNTS2=$(curl -sf "$BASE/training-runs/$RUN2/dataset.csv" | grep '^ex2-' | cut -d, -f4 | sort -u)
echo "run $RUN2 dataset now contains $EXTRA2 ex2-extra rows (must be 50)"
echo "distinct amounts among ex2-* rows of run $RUN2: $(echo $AMOUNTS2) (must be 255 = 250 + 5)"
[ "$EXTRA2" = "50" ] && [ "$AMOUNTS2" = "255" ] && echo "RUN 2 SCOPE OK"

echo "== 9. run $RUN1 dataset is immutable (still all 100) =="
AMOUNTS1_AGAIN=$(curl -sf "$BASE/training-runs/$RUN1/dataset.csv" | grep '^ex2-' | cut -d, -f4 | sort -u)
[ "$AMOUNTS1_AGAIN" = "100" ] && echo "OK: run $RUN1 unchanged"
