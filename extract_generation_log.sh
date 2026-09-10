#!/bin/bash

echo "================================================================================"
echo "                    EXTRACTING FUNDOS v2.1 PROFILE GENERATION LOGS"
echo "================================================================================"
echo ""

LOG_FILE="/d01/fundos/logs/celery-worker.log"

if [ ! -f "$LOG_FILE" ]; then
    echo "✗ Log file not found: $LOG_FILE"
    exit 1
fi

echo "Log file: $LOG_FILE"
echo "Size: $(du -h $LOG_FILE | cut -f1)"
echo "Lines: $(wc -l < $LOG_FILE)"
echo ""

# Extract profile generation logs
echo "Extracting profile.generate runs..."
echo ""

# Find all profile.generate entries
grep "profile.generate" "$LOG_FILE" | tail -20 > /tmp/profile_runs.txt

echo "Recent profile generation tasks:"
cat /tmp/profile_runs.txt
echo ""

# Get the most recent profile.generate task ID
LATEST_TASK=$(grep "profile.generate" "$LOG_FILE" | tail -1 | grep -oP "profile.generate\[\K[^]]*")

if [ -z "$LATEST_TASK" ]; then
    echo "No profile generation tasks found in logs"
    exit 1
fi

echo "Latest task ID: $LATEST_TASK"
echo ""

# Extract all lines for this task
echo "Extracting full log for task: $LATEST_TASK"
echo ""

grep "$LATEST_TASK" "$LOG_FILE" > /tmp/fundos_latest_generation.log

echo "Extracted $(wc -l < /tmp/fundos_latest_generation.log) lines"
echo ""

# Show the extracted log
echo "================================================================================"
echo "                    FULL PROFILE GENERATION LOG"
echo "================================================================================"
echo ""
cat /tmp/fundos_latest_generation.log
echo ""
echo "================================================================================"
echo ""

# Save to file
cp /tmp/fundos_latest_generation.log fundos_generation_$(date +%Y%m%d_%H%M%S).log
echo "Saved to: fundos_generation_$(date +%Y%m%d_%H%M%S).log"
echo ""

