#!/bin/bash

set -e

echo "================================================================================"
echo "                    RESTARTING ALL FUNDOS SERVICES"
echo "================================================================================"
echo ""

# Stop services
echo "Stopping services..."
sudo systemctl stop fundos-web fundos-worker fundos-beat
sleep 5
echo "✓ Services stopped"
echo ""

# Start services
echo "Starting services..."
sudo systemctl start fundos-web fundos-worker fundos-beat
sleep 10
echo "✓ Services started"
echo ""

# Verify all running
echo "Verifying services..."
echo ""
for service in fundos-web fundos-worker fundos-beat; do
    status=$(systemctl is-active $service)
    if [ "$status" = "active" ]; then
        echo "✓ $service: $status"
    else
        echo "✗ $service: $status"
    fi
done
echo ""
echo "================================================================================"
echo "                    ALL SERVICES RESTARTED"
echo "================================================================================"
echo ""
