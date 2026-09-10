#!/bin/bash

echo "================================================================================"
echo "                    FUNDOS v2.1 - COMPLETE DIAGNOSTIC MONITOR"
echo "================================================================================"
echo ""
echo "Log file: /d01/fundos/logs/celery-worker.log"
echo ""
echo "Monitoring in real-time - shows ALL output (not filtered)"
echo "Press Ctrl+C to stop"
echo ""
echo "================================================================================"
echo ""

# Get initial line count
INITIAL_LINES=$(wc -l < /d01/fundos/logs/celery-worker.log)

# Show last 20 lines as context
echo "=== CONTEXT (Last 20 lines before monitoring) ==="
tail -20 /d01/fundos/logs/celery-worker.log
echo ""
echo "=== NEW LOG ENTRIES (Real-time) ==="
echo ""

# Tail and show everything with timestamps
tail -f /d01/fundos/logs/celery-worker.log | while IFS= read -r line; do
    # Add timestamp
    timestamp=$(date '+%H:%M:%S')
    
    # Color coding for important patterns
    if echo "$line" | grep -q "GEN\["; then
        # v2.1 diagnostic entry - CYAN
        echo -e "\033[36m${timestamp} ${line}\033[0m"
    elif echo "$line" | grep -q "research.batch"; then
        # Research batch - BLUE
        echo -e "\033[34m${timestamp} ${line}\033[0m"
    elif echo "$line" | grep -q "profile.generate\|step=\|preflight\|synthesis\|assessment\|complete"; then
        # Profile generation flow - GREEN
        echo -e "\033[32m${timestamp} ${line}\033[0m"
    elif echo "$line" | grep -q "ERROR\|FAIL\|FATAL\|Exception"; then
        # Errors - RED
        echo -e "\033[31m${timestamp} ${line}\033[0m"
    else
        # Default - WHITE
        echo "${timestamp} ${line}"
    fi
done

