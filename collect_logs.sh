#!/bin/bash

set -e

echo "================================================================================"
echo "                    COLLECTING ALL LOGS FOR ANALYSIS"
echo "================================================================================"
echo ""

# Create temp directory for logs
TEMP_DIR="/tmp/fundos-logs-collection-$$"
mkdir -p "$TEMP_DIR"

echo "Collection directory: $TEMP_DIR"
echo ""

# Step 1: Copy celery worker log
echo "[1/4] Copying celery-worker.log..."
cp /d01/fundos/logs/celery-worker.log "$TEMP_DIR/01_celery-worker.log" 2>/dev/null || echo "✗ Not found"

# Step 2: Extract latest profile generation logs
echo "[2/4] Extracting profile generation logs..."
cd /d01/fundos/src
source /d01/fundos/app/venv/bin/activate 2>/dev/null
set -a; source /etc/fundos/fundos.env; set +a 2>/dev/null

# Get latest profile runs from log
grep "profile.generate" /d01/fundos/logs/celery-worker.log > "$TEMP_DIR/02_profile-generate-tasks.log" 2>/dev/null || echo "# No tasks found" > "$TEMP_DIR/02_profile-generate-tasks.log"

# Get last 100 lines (recent activity)
tail -100 /d01/fundos/logs/celery-worker.log > "$TEMP_DIR/03_recent-activity.log"

# Step 3: Get database metadata (skip if error)
echo "[3/4] Extracting database metadata..."
python manage.py shell 2>/dev/null << 'PYEOF' > "$TEMP_DIR/04_database-metadata.txt" || echo "Database extraction skipped" > "$TEMP_DIR/04_database-metadata.txt"
from fundos.profile.models import ProfileGenerationRun
from fundos.llm.models import LLMCallLog
import json

print("=" * 80)
print("LATEST PROFILE GENERATION RUNS")
print("=" * 80)
print()

runs = ProfileGenerationRun.objects.order_by('-created_at')[:5]

for i, run in enumerate(runs, 1):
    print(f"Run #{i}")
    print(f"  Run ID: {run.run_id}")
    print(f"  Status: {run.status}")
    print(f"  Duration (ms): {run.duration_ms}")
    print(f"  Started: {run.started_at}")
    print(f"  Completed: {run.completed_at}")
    print(f"  LLM Calls: {run.llm_calls}")
    print(f"  Sections: {run.sections_count}")
    print(f"  Fields: {run.fields_count}")
    print(f"  Completeness: {run.completeness_pct}%")
    print(f"  Cost (₹): {run.total_cost_inr}")
    print()

print("=" * 80)
print("LLM CALL DETAILS (Latest Run)")
print("=" * 80)
print()

if runs:
    latest = runs[0]
    calls = LLMCallLog.objects.filter(run_id=latest.run_id)
    
    for call in calls:
        print(f"Role: {call.role}")
        print(f"  Status: {call.status}")
        print(f"  Endpoint: {call.endpoint_code}")
        print(f"  Latency: {call.latency_ms}ms")
        print(f"  Prompt Tokens: {call.prompt_tokens}")
        print(f"  Completion Tokens: {call.completion_tokens}")
        print(f"  Cost: ₹{call.cost_inr}")
        print()
PYEOF

# Step 4: Get diagnostics (if available)
echo "[4/4] Extracting diagnostics..."
python manage.py shell 2>/dev/null << 'PYEOF' > "$TEMP_DIR/05_diagnostics.json" || echo "{}" > "$TEMP_DIR/05_diagnostics.json"
from fundos.profile.models import ProfileGenerationRun
import json

run = ProfileGenerationRun.objects.order_by('-created_at').first()

if run and run.diagnostics:
    print(json.dumps(run.diagnostics, indent=2))
else:
    print("{}")
PYEOF

# List what was collected
echo ""
echo "Files collected:"
ls -lh "$TEMP_DIR/"
echo ""

# Create tar archive
TAR_NAME="fundos-logs-$(date +%Y%m%d-%H%M%S).tar.gz"
TAR_PATH="/tmp/$TAR_NAME"

echo "Creating archive..."
cd /tmp
tar -czf "$TAR_NAME" "fundos-logs-collection-$$/"

echo ""
echo "================================================================================"
echo "                    COLLECTION COMPLETE"
echo "================================================================================"
echo ""
echo "Archive: $TAR_PATH"
echo "Size: $(du -h $TAR_PATH | cut -f1)"
echo ""
echo "Contents:"
tar -tzf "$TAR_PATH" | sed 's/^/  /'
echo ""
echo "Download this file for analysis:"
echo "  $TAR_PATH"
echo ""

# Cleanup
rm -rf "$TEMP_DIR"

