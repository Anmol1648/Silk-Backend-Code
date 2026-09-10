#!/bin/bash

echo "================================================================================"
echo "                    FINDING ALL LOG FILES"
echo "================================================================================"
echo ""

echo "Searching for log files..."
echo ""

# Search all possible log locations
echo "=== LOG FILES IN /d01/fundos/logs/ ==="
ls -lh /d01/fundos/logs/ 2>/dev/null || echo "Directory not found"
echo ""

echo "=== LOG FILES IN /d01/fundos/shared/logs/ ==="
ls -lh /d01/fundos/shared/logs/ 2>/dev/null || echo "Directory not found"
echo ""

echo "=== LOG FILES IN /var/log/fundos/ ==="
ls -lh /var/log/fundos/ 2>/dev/null || echo "Directory not found"
echo ""

echo "=== LOG FILES IN /home/fundos/logs/ ==="
ls -lh /home/fundos/logs/ 2>/dev/null || echo "Directory not found"
echo ""

echo "=== ALL .log FILES IN /d01/fundos/ ==="
find /d01/fundos -name "*.log" -type f 2>/dev/null | head -20
echo ""

echo "=== ALL .log FILES IN /d01/fundos/current (symlink check) ==="
if [ -L /d01/fundos/current/logs ]; then
    echo "Symlink target:"
    ls -lh /d01/fundos/current/logs/
fi
echo ""

echo "=== CURRENT SIZE OF CELERY-WORKER.LOG ==="
wc -l /d01/fundos/logs/celery-worker.log 2>/dev/null || echo "Not found"
du -h /d01/fundos/logs/celery-worker.log 2>/dev/null || echo "Not found"
echo ""

echo "================================================================================"

