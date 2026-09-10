#!/usr/bin/env bash
#
# FundOS backend deploy — FLAT /d01/fundos/src layout.
#
# Replaces deploy-backend.sh, whose structure validation
# ("$REL/FUNDOS/fundos-backend/manage.py") can never pass once releases are
# no longer nested under FUNDOS/fundos-backend.
#
# WHAT IS KEPT FROM THE OLD SCRIPT, AND WHY
#   * venv sanity      — a 3.13 venv silently miscompiles against Django 4.2
#   * requirements diff — pip is slow; only run it when the file changed
#   * migration count   — apply only what is pending, and say how many
#   * health gate       — 401 from an authenticated endpoint proves the app
#                         booted AND its middleware chain is intact
#   * automatic rollback on a failed health check
#
# WHAT CHANGED
#   A flat tree has no symlink to flip, so the cutover is two renames:
#       src -> src.previous     (the rollback target)
#       src.new -> src
#   Both are metadata-only operations on the same filesystem, so the window
#   where /d01/fundos/src does not exist is sub-millisecond. Services are
#   stopped across it regardless.
#
# Usage: deploy-backend-src.sh <artifact.tar.gz> [--skip-migrate]
set -euo pipefail

ART="${1:?usage: deploy-backend-src.sh <artifact.tar.gz> [--skip-migrate]}"
SKIP_MIGRATE="${2:-}"

BASE=/d01/fundos
SRC=$BASE/src
PREV=$BASE/src.previous
NEW=$BASE/src.new
VENV=$BASE/app/venv                       # 3.11 venv — the ONLY correct one
ENVF=/etc/fundos/fundos.env
SVCS=(fundos-web fundos-worker fundos-beat)
HEALTH_URL=https://befundos.gyain.com/api/v1/me/contexts

log(){ echo -e "\n[$(date +%H:%M:%S)] $*"; }
fail(){ echo "FATAL: $*" >&2; exit 1; }

[ -f "$ART" ] || fail "artifact not found: $ART"
[ -f "$ENVF" ] || fail "env file not found: $ENVF"

log "1/9 venv sanity"
"$VENV/bin/python" -V | grep -q '3\.11' || fail "$VENV is not Python 3.11"
"$VENV/bin/python" -V

log "2/9 unpack -> $NEW"
sudo rm -rf "$NEW"
sudo -u fundos mkdir -p "$NEW"
sudo -u fundos tar -xzf "$ART" -C "$NEW"

log "3/9 validate structure"
[ -f "$NEW/manage.py" ] || fail "manage.py not at the root of $NEW — this script expects a FLAT archive"
[ -d "$NEW/fundos" ]    || fail "fundos/ package missing from the archive"

log "4/9 carry over runtime state"
# var/ holds locally-stored uploads when FUNDOS_STORAGE_BACKEND=local. It is
# deliberately excluded from the artifact (it is data, not code), so it has to
# be carried forward or those files become unreachable.
if [ -d "$SRC/var" ]; then
  sudo -u fundos cp -a "$SRC/var" "$NEW/var"
  echo "  carried var/ forward"
else
  echo "  no var/ in the current tree — nothing to carry"
fi
[ -f "$SRC/celerybeat-schedule" ] && sudo -u fundos cp -a "$SRC/celerybeat-schedule" "$NEW/" || true

log "5/9 check dependencies"
DO_PIP=1
if [ -f "$SRC/requirements.txt" ] && diff -q "$SRC/requirements.txt" "$NEW/requirements.txt" >/dev/null 2>&1; then
  DO_PIP=0; echo "  requirements unchanged"
else
  echo "  requirements changed -> will pip install"
fi

log "6/9 stop services"
sudo systemctl stop "${SVCS[@]}"

log "7/9 cut over"
sudo rm -rf "$PREV"
[ -d "$SRC" ] && sudo mv "$SRC" "$PREV"
sudo mv "$NEW" "$SRC"
sudo chown -R fundos:fundos "$SRC"
echo "  $SRC is live; previous tree kept at $PREV"

log "8/9 pip / migrate / static"
cd "$SRC"
set -a; source "$ENVF"; set +a
if [ "$DO_PIP" = 1 ]; then
  sudo -u fundos "$VENV/bin/pip" install -q -r requirements.txt
  echo "  dependencies installed"
fi
UNAPPLIED=$(sudo -E -u fundos "$VENV/bin/python" manage.py showmigrations --plan 2>/dev/null | grep -c '\[ \]' || true)
if [ "$SKIP_MIGRATE" = "--skip-migrate" ]; then
  echo "  --skip-migrate set; $UNAPPLIED unapplied migration(s) NOT applied"
elif [ "$UNAPPLIED" -gt 0 ]; then
  echo "  applying $UNAPPLIED migration(s)"
  sudo -E -u fundos "$VENV/bin/python" manage.py migrate --noinput
else
  echo "  no migrations to apply"
fi
sudo -E -u fundos "$VENV/bin/python" manage.py collectstatic --noinput >/dev/null
echo "  static collected"

log "9/9 start + health check"
sudo systemctl start "${SVCS[@]}"
sleep 5
FAILED=0
for s in "${SVCS[@]}"; do
  systemctl is-active --quiet "$s" && echo "  OK  $s" || { echo "  ERR $s NOT active"; FAILED=1; }
done
CODE=$(curl -sk -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)
[ "$CODE" = "401" ] && echo "  OK  API 401" || { echo "  ERR API returned $CODE (want 401)"; FAILED=1; }

if [ "$FAILED" -ne 0 ]; then
  log "HEALTH CHECK FAILED — rolling back"
  sudo rm -rf "$BASE/src.failed"
  sudo mv "$SRC" "$BASE/src.failed"
  sudo mv "$PREV" "$SRC"
  sudo systemctl restart "${SVCS[@]}"
  sleep 5
  CODE=$(curl -sk -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)
  echo "  after rollback the API returns $CODE"
  fail "deploy rolled back; the bad tree is at $BASE/src.failed for inspection"
fi

log "SUCCESS — /d01/fundos/src is live. Rollback tree: $PREV"
