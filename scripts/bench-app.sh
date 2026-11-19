#!/bin/zsh
# App speed benchmark: launch Beacon (Mac) in bench mode; it opens Library, times launch until
# 12 pictures show, scrolls 12 seconds, writes /tmp/beacon-bench.json and quits.
#   scripts/bench-app.sh "what changed"
set -euo pipefail
cd "${0:A:h}/.."
rm -f /tmp/beacon-bench.json
osascript -e 'quit app "Beacon"' 2>/dev/null || true
pkill -x Beacon 2>/dev/null || true  # leftover bench instances skew the next run
sleep 1
open -n --env BEACON_BENCH=${BENCH_MODE:-1} ~/Applications/Beacon.app
for i in {1..90}; do [[ -f /tmp/beacon-bench.json ]] && break; sleep 1; done
[[ -f /tmp/beacon-bench.json ]] || { echo "no result after 90 s" >&2; exit 1; }
cat /tmp/beacon-bench.json; echo
python3 -c "
import json, sys
from datetime import datetime, timezone
app = json.load(open('/tmp/beacon-bench.json'))
with open('docs/perf/bench.jsonl', 'a') as log:
    log.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'label': sys.argv[1], 'app_mac': app}) + '\n')
" "${1:-}"
