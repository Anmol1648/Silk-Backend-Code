#!/usr/bin/env bash
# Reconstruct the full company-profile generation trace on the PROD server —
# the same detailed GEN[...] / PIPELINE[...] step lines you see locally.
#
# Usage:
#   trace_company_profile.sh latest
#   trace_company_profile.sh <request_id | company_id | tenant_id | run_id | any token>
#   trace_company_profile.sh <token> --raw     # emit the raw JSON log lines, unformatted
#
# It reads BOTH the celery worker logfile (incl. rotated .log.* / .gz) AND
# journald, because depending on the logging config the detailed lines may
# land in either sink. Run as root, or as a user in the systemd-journal group.
set -uo pipefail

TOKEN="${1:?usage: trace_company_profile.sh <latest|request_id|company_id|...> [--raw]}"
RAW="${2:-}"

LOGDIR=/d01/fundos/logs
FILES=("$LOGDIR"/celery-worker.log "$LOGDIR"/celery-worker.log.* \
       "$LOGDIR"/generation.log   "$LOGDIR"/generation.log.*)

gather() {
  for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    case "$f" in
      *.gz) zcat -- "$f" 2>/dev/null ;;
      *)    cat  -- "$f" 2>/dev/null ;;
    esac
  done
  # systemd journal for the worker (and web, in case an onboard ran eagerly there)
  journalctl -u fundos-worker -u fundos-web --no-pager -o cat 2>/dev/null || true
}

TMP=$(mktemp)
gather > "$TMP"

# Resolve the request_id(s) the trace is keyed on.
if [ "$TOKEN" = "latest" ]; then
  RIDS=$(grep -a -E 'preflight\.config|pipeline started' "$TMP" \
         | grep -ao '"request_id":"[^"]*"' | sed 's/.*:"//; s/"//' \
         | sort -u | tail -1)
else
  RIDS=$(grep -a -- "$TOKEN" "$TMP" \
         | grep -ao '"request_id":"[^"]*"' | sed 's/.*:"//; s/"//' \
         | sort -u)
fi

if [ -z "${RIDS:-}" ]; then
  echo "No request_id found for token: $TOKEN" >&2
  echo "If nothing matches even with a valid company/tenant id, the detailed" >&2
  echo "loggers are probably off in prod — run check_profile_logging.sh." >&2
  rm -f "$TMP"; exit 1
fi
echo "# request_id(s): $(echo "$RIDS" | tr '\n' ' ')" >&2

# Pull every line for those request_ids, sort by ts, print.
python3 - "$TMP" "$RAW" "$RIDS" <<'PY'
import sys, json
tmp, raw = sys.argv[1], sys.argv[2]
rids = set(sys.argv[3].split())
rows = []
with open(tmp, errors='replace') as fh:
    for ln in fh:
        if not any(r in ln for r in rids):
            continue
        i = ln.find('{')
        if i < 0:
            continue
        try:
            o = json.loads(ln[i:])
        except Exception:
            continue
        if o.get('request_id') not in rids:
            continue
        ts = o.get('ts') or o.get('time') or ''      # local uses ts, prod uses time
        rows.append((ts, o, ln.rstrip('\n')))
rows.sort(key=lambda r: r[0])
for ts, o, raw_ln in rows:
    if raw == '--raw':
        print(raw_ln)
    else:
        msg = o.get('msg') or o.get('message') or ''  # local uses msg, prod uses message
        print(f"{ts:23}  {o.get('level',''):7} "
              f"{o.get('logger',''):22} {msg}")
print(f"\n# {len(rows)} line(s)", file=sys.stderr)
PY
rm -f "$TMP"
