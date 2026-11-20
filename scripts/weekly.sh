#!/bin/zsh
# Weekly Beacon run (launchd: com.example.beacon-weekly). Mondays 8:00.
# The app's Sync button runs this same script. It syncs every source enabled in
# config.json, scores new saves, refills each project's top 25, then sweeps.
# About $0.05 of Jev per week. No second schedule.
set -u
# A failed score must not hide behind the summary line it pipes into.
set -o pipefail
# ~/.local/bin holds claude, pnpm and with-secret; launchd starts jobs without it. Last, so it adds
# tools without replacing ones already found (its node is older than Homebrew's).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"
cd "${0:A:h}/.." || exit 1
mkdir -p logs
# The Monday job and the app's Sync button both run this; a second copy exits 75 instead of overlapping.
[[ -n "${BEACON_LOCKED:-}" ]] || BEACON_LOCKED=1 exec /usr/bin/lockf -t 0 logs/weekly.lock /bin/zsh "${0:A}"
exec >>logs/weekly.log 2>&1
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) weekly run"

# One source or stage failing must not skip the others, and it must not look like success.
sync_ok=1
failed=()
keychain_sync=0
if [[ -x "$HOME/.local/bin/with-secret" ]] &&
  "$HOME/.local/bin/with-secret" --check BEACON_X_AUTH_TOKEN BEACON_X_CT0 >/dev/null 2>&1; then
  keychain_sync=1
fi
BEACON_X_KEYCHAIN="$keychain_sync" python3 beacon.py sync || sync_ok=0
[[ $keychain_sync == 1 ]] || echo "X sync used no Keychain sign-in; run scripts/x-keys once so the Monday job can reach X."

TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-$(/usr/bin/security find-generic-password -s typesafe-personal-dev -a "${TYPESAFE_KEYCHAIN_ACCOUNT:-$USER}" -w 2>/dev/null)}"
if [[ -z "$TYPESAFE_API_KEY" ]]; then
  echo "TypeSafe key unavailable from Keychain; skipping scoring and routing."
  exit 1
fi
export TYPESAFE_API_KEY
python3 beacon.py classify --provider typesafe --allow-upload --limit 1000 \
  --routing-evaluation runs/2026-09-20-routing/live-v2.json --since "$(date -u -v-90d +%Y-%m-%d)" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print("scored", d["live_model_calls"], "held", d["held"], "errors", d["errors"], "cost_usd", round(d["estimated_cost_usd"], 4))' || failed+=(classify)
python3 beacon.py areas --allow-upload --limit 1000 || failed+=(areas)
# Owner tags no project or area names: a model proposes where they point, sent only when a term or a project is new.
python3 beacon.py vocab propose || failed+=(vocab)
unset TYPESAFE_API_KEY

python3 beacon.py route-top || failed+=(route-top)
python3 beacon.py stack --allow-upload || failed+=(stack)
python3 beacon.py pile release --per 3 || failed+=(pile)
python3 beacon.py gist || failed+=(gist)
# Beacon passes what it judged noise and sends up to 45 reads to Readwise Reader; Do picks wait in Triage.
python3 beacon.py autopilot || failed+=(autopilot)
python3 beacon.py follow-up || failed+=(follow-up)
# On-device (Apple Vision + NaturalLanguage): new picture saves become searchable by meaning and similarity.
python3 beacon.py vectors --limit 2000 || echo "vectors skipped; the error above says why (helper not built, or SigLIP model missing)"
# Plan any Do items that lack a brief (or whose last attempt failed), one agent run per project.
python3 beacon.py brief --allow-upload || failed+=(brief)
# Sweep only ages out long-untouched items by date, so it is safe after any failed stage.
python3 beacon.py sweep || failed+=(sweep)
# Every part that runs on its own (jobs, phone server, sign-ins, deploys); a failure sends a notification.
# Before "=== done", because the app's Sync shows the last line.
if [[ -x scripts/doctor ]]; then
  scripts/python scripts/doctor --notify || echo "Doctor found a problem; see the FAIL lines above."
fi
echo "=== done"
(( sync_ok )) || failed=(sync $failed)
if (( $#failed )); then
  # Last line on purpose: the app's Sync shows it.
  echo "Didn't finish: ${(j:, :)failed}. Earlier lines say why; the other steps still ran."
  exit 1
fi
