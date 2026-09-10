#!/bin/bash

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Get tarball path from parameter
TARBALL="${1}"

if [ -z "$TARBALL" ]; then
    echo -e "${RED}✗ Usage: bash deploy.sh /path/to/tarball.tar.gz${NC}"
    exit 1
fi

if [ ! -f "$TARBALL" ]; then
    echo -e "${RED}✗ Tarball not found: $TARBALL${NC}"
    exit 1
fi

echo "================================================================================"
echo "                    FUNDOS BACKEND DEPLOYMENT SCRIPT"
echo "================================================================================"
echo ""
echo -e "${YELLOW}Tarball: $TARBALL${NC}"
echo "Size: $(du -h $TARBALL | cut -f1)"
echo ""

# Step 1: Verify tarball structure
echo -e "${YELLOW}[1/9] Verifying tarball structure...${NC}"
HAS_MANAGE=$(tar -tzf "$TARBALL" | grep -c "^manage.py" || echo 0)

if [ $HAS_MANAGE -eq 0 ]; then
    echo -e "${RED}✗ Invalid tarball: No manage.py found${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Tarball structure valid${NC}"
echo ""

# Step 2: Create backup
echo -e "${YELLOW}[2/9] Backing up current code...${NC}"
cd /d01/fundos
BACKUP_NAME="src.backup-$(date +%Y%m%d-%H%M%S)"
sudo cp -r src "$BACKUP_NAME"
echo -e "${GREEN}✓ Backup created: $BACKUP_NAME${NC}"
echo ""

# Step 3: Stop services
echo -e "${YELLOW}[3/9] Stopping services...${NC}"
sudo systemctl stop fundos-web fundos-worker fundos-beat
sleep 5
echo -e "${GREEN}✓ Services stopped${NC}"
echo ""

# Step 4: Extract tarball
echo -e "${YELLOW}[4/9] Extracting tarball...${NC}"
TEMP_EXTRACT="/tmp/fundos-extract-$$"
mkdir -p "$TEMP_EXTRACT"
tar -xzf "$TARBALL" -C "$TEMP_EXTRACT"
echo -e "${GREEN}✓ Tarball extracted${NC}"
echo ""

# Step 5: Deploy
echo -e "${YELLOW}[5/9] Deploying code...${NC}"
sudo rm -rf /d01/fundos/src
sudo mv "$TEMP_EXTRACT" /d01/fundos/src
sudo chown -R fundos:fundos /d01/fundos/src
echo -e "${GREEN}✓ Code deployed${NC}"
echo ""

# Step 6: Verify basic structure
echo -e "${YELLOW}[6/9] Verifying deployment structure...${NC}"
cd /d01/fundos/src

if [ ! -f "manage.py" ]; then
    echo -e "${RED}✗ Deployment failed: manage.py not found${NC}"
    echo "Rolling back..."
    sudo rm -rf /d01/fundos/src
    sudo mv /d01/fundos/$BACKUP_NAME /d01/fundos/src
    sudo systemctl start fundos-web fundos-worker fundos-beat
    exit 1
fi

if [ ! -d "fundos" ]; then
    echo -e "${RED}✗ Deployment failed: fundos/ directory not found${NC}"
    echo "Rolling back..."
    sudo rm -rf /d01/fundos/src
    sudo mv /d01/fundos/$BACKUP_NAME /d01/fundos/src
    sudo systemctl start fundos-web fundos-worker fundos-beat
    exit 1
fi

echo -e "${GREEN}✓ Structure verified (manage.py, fundos/ present)${NC}"
echo ""

# Step 7: Verify shell scripts are present
echo -e "${YELLOW}[7/9] Verifying shell scripts...${NC}"
cd /d01/fundos/src

SH_FILES=$(find . -maxdepth 2 -name "*.sh" -type f | wc -l)
SH_LIST=$(find . -maxdepth 2 -name "*.sh" -type f | head -20)

if [ $SH_FILES -eq 0 ]; then
    echo -e "${YELLOW}⚠ Warning: No .sh files found in deployment${NC}"
    echo "Checking backup for reference..."
    BACKUP_SH=$(find /d01/fundos/$BACKUP_NAME -maxdepth 2 -name "*.sh" -type f | wc -l)
    echo "  Backup had: $BACKUP_SH .sh files"
    if [ $BACKUP_SH -gt 0 ]; then
        echo -e "${RED}✗ DEPLOYMENT FAILED: .sh files missing${NC}"
        echo "Rolling back..."
        sudo rm -rf /d01/fundos/src
        sudo mv /d01/fundos/$BACKUP_NAME /d01/fundos/src
        sudo systemctl start fundos-web fundos-worker fundos-beat
        exit 1
    fi
else
    echo -e "${GREEN}✓ Found $SH_FILES shell scripts${NC}"
    echo "Shell scripts:"
    echo "$SH_LIST" | sed 's/^/  /'
fi

echo ""

# Step 8: Start services
echo -e "${YELLOW}[8/9] Starting services...${NC}"
sudo systemctl start fundos-web fundos-worker fundos-beat
sleep 10
echo -e "${GREEN}✓ Services started${NC}"
echo ""

# Step 9: Verify services
echo -e "${YELLOW}[9/9] Verifying services...${NC}"
for service in fundos-web fundos-worker fundos-beat; do
    status=$(systemctl is-active "$service")
    if [ "$status" = "active" ]; then
        echo -e "  ${GREEN}✓ $service: active${NC}"
    else
        echo -e "  ${RED}✗ $service: $status${NC}"
    fi
done
echo ""

echo "================================================================================"
echo "                    DEPLOYMENT COMPLETE"
echo "================================================================================"
echo ""
echo "Backup: /d01/fundos/$BACKUP_NAME"
echo "Code:   /d01/fundos/src"
echo "Shell scripts: $SH_FILES found"
echo ""
