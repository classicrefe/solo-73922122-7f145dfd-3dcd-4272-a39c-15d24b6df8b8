#!/usr/bin/env bash
# Example 3 — restart resume: the service is restarted mid-run and
# continues from the samples already completed, with the SAME frozen
# revision scope. No sample is recomputed, no later-submitted batch leaks
# into the run — not even a backdated correction uploaded right after the
# restart.
#
# Expected: all 20000 ex3-* samples of the run amount to 7 (the base
# revision), although a past-visible correction (77) is uploaded after
# the restart while the run is still going. A new run then shows 77.
#
# Tip: make the window easy to observe by slowing the worker:
#   RUN_CHUNK_DELAY_MS=100 docker compose up -d --build
set -euo pipefail
cd "$(dirname "$0")"
source ./_common.sh
require_app

python3 - <<'EOF'
import csv
with open('/tmp/example3_samples.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['sample_id', 'account_id', 'prediction_at_utc'])
    for i in range(20000):
        w.writerow([f'ex3-{i:05d}', 'acct-EX3', '2026-11-15T12:00:00Z'])
print('generated 20000 samples -> /tmp/example3_samples.csv')
EOF

echo "== 0. import base revision for acct-EX3 (ex3-p1 = 7, visible before T) =="
post_csv /payment-revision-batches data/revisions_ex3_base.csv; echo

echo "== 1. upload 20000 samples, create run =="
post_csv /sample-batches /tmp/example3_samples.csv; echo
RUN_JSON=$(curl -sf -X POST "$BASE/training-runs")
RUN=$(json_field "$RUN_JSON" "d['run_id']")
TOTAL=$(json_field "$RUN_JSON" "d['total_samples']")
echo "run $RUN created: total_samples=$TOTAL revision_watermark_batch=$(json_field "$RUN_JSON" "d['revision_watermark_batch']")"

echo "== 2. wait for partial progress =="
BEFORE=""
for _ in $(seq 1 120); do
  set -- $(run_status "$RUN")
  st=$1; done_n=$2
  if [ "$st" = "completed" ]; then
    echo "run completed before restart could happen (${done_n}/${TOTAL})."
    echo "Re-run with a slower worker: RUN_CHUNK_DELAY_MS=100 docker compose up -d"
    exit 0
  fi
  if [ "$done_n" -gt 0 ] && [ "$done_n" -lt "$TOTAL" ]; then BEFORE="$done_n"; break; fi
  sleep 1
done
echo "progress before restart: $BEFORE / $TOTAL"

echo "== 3. docker compose restart app =="
( cd .. && docker compose restart app )
for _ in $(seq 1 60); do curl -sf "$BASE/health" >/dev/null 2>&1 && break; sleep 1; done
require_app

echo "== 4. progress right after restart (must resume, not restart from 0) =="
set -- $(run_status "$RUN"); echo "status=$1 done=$2 total=$3"
[ "$2" -ge "$BEFORE" ] && echo "OK: done_samples kept ($2 >= $BEFORE)"

echo "== 5. backdated correction ex3-p1#2 (77, visible before T) uploaded now —"
echo "      the resumed run must still use its ORIGINAL revision scope =="
post_csv /payment-revision-batches data/revisions_ex3_backdated.csv; echo

echo "== 6. wait for completion and verify the dataset =="
wait_run "$RUN"
ROWS=$(curl -sf "$BASE/training-runs/$RUN/dataset.csv" | tail -n +2 | wc -l)
DISTINCT=$(curl -sf "$BASE/training-runs/$RUN/dataset.csv" | tail -n +2 | cut -d, -f1 | sort -u | wc -l)
AMOUNTS=$(curl -sf "$BASE/training-runs/$RUN/dataset.csv" | grep '^ex3-' | cut -d, -f4 | sort -u)
echo "dataset rows: $ROWS (expect $TOTAL), distinct sample_ids: $DISTINCT (no duplicates)"
echo "distinct amounts among ex3-* rows: $(echo $AMOUNTS) (must be 7 — scope frozen across restart)"
[ "$ROWS" = "$TOTAL" ] && [ "$DISTINCT" = "$TOTAL" ] && [ "$AMOUNTS" = "7" ] && echo "RESTART RESUME OK"

echo "== 7. a new run picks the correction up =="
RUN2=$(curl -sf -X POST "$BASE/training-runs" | python3 -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
wait_run "$RUN2"
AMOUNTS2=$(curl -sf "$BASE/training-runs/$RUN2/dataset.csv" | grep '^ex3-' | cut -d, -f4 | sort -u)
echo "distinct amounts among ex3-* rows of run $RUN2: $(echo $AMOUNTS2) (must be 77)"
[ "$AMOUNTS2" = "77" ] && echo "NEXT RUN SCOPE OK"
