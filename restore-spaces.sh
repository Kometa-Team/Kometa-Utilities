#!/bin/bash
set -e

# DigitalOcean Spaces Restore Script
# Restores backup from DigitalOcean Spaces to local filesystem

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Check if Spaces is configured
if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] || [ -z "$S3_BUCKET_NAME" ]; then
    echo "❌ Error: Spaces credentials not configured in .env file"
    echo "Required variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME"
    exit 1
fi

# Configure AWS CLI for Spaces
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-nyc3}"

# Set endpoint for DigitalOcean Spaces
if [ -n "$S3_ENDPOINT" ]; then
    ENDPOINT_URL="--endpoint-url=$S3_ENDPOINT"
    echo "🌐 Using DigitalOcean Spaces endpoint: $S3_ENDPOINT"
else
    ENDPOINT_URL=""
    echo "🌐 Using AWS S3"
fi

echo "📋 Available backups in Spaces:"
echo ""

# List available backups
aws s3 ls \
    $ENDPOINT_URL \
    "s3://$S3_BUCKET_NAME/" \
    --human-readable \
    | grep "anidb-backup-" \
    | sort -r \
    | head -n 10

echo ""
echo "Enter the backup filename to restore (or 'latest' for most recent):"
read -r BACKUP_FILE

# If 'latest', get the most recent backup
if [ "$BACKUP_FILE" = "latest" ]; then
    BACKUP_FILE=$(aws s3 ls $ENDPOINT_URL "s3://$S3_BUCKET_NAME/" \
        | grep "anidb-backup-" \
        | sort -r \
        | head -n 1 \
        | awk '{print $4}')
    echo "Selected latest backup: $BACKUP_FILE"
fi

# Validate backup exists
if [ -z "$BACKUP_FILE" ]; then
    echo "❌ Error: No backup file selected"
    exit 1
fi

# Confirmation
echo ""
echo "⚠️  WARNING: This will replace your current database and data files!"
echo "Backup to restore: $BACKUP_FILE"
echo ""
read -p "Are you sure you want to continue? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "❌ Restore cancelled"
    exit 0
fi

# Create restore directory
RESTORE_DIR="./restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESTORE_DIR"

# Download backup
echo ""
echo "⬇️  Downloading backup from Spaces..."
aws s3 cp \
    $ENDPOINT_URL \
    "s3://$S3_BUCKET_NAME/$BACKUP_FILE" \
    "$RESTORE_DIR/$BACKUP_FILE"

if [ $? -ne 0 ]; then
    echo "❌ Failed to download backup"
    rm -rf "$RESTORE_DIR"
    exit 1
fi

echo "✅ Downloaded successfully"

# Extract backup
echo "📦 Extracting backup..."
tar -xzf "$RESTORE_DIR/$BACKUP_FILE" -C "$RESTORE_DIR"

# Stop services
echo "🛑 Stopping services..."
docker compose down

# Backup current data
echo "💾 Backing up current data to ./pre-restore-backup..."
mkdir -p ./pre-restore-backup
[ -f "./database.db" ] && cp "./database.db" "./pre-restore-backup/database.db.bak"
[ -d "./data" ] && cp -r "./data" "./pre-restore-backup/data.bak"

# Restore database
if [ -f "$RESTORE_DIR/database.db" ]; then
    echo "📥 Restoring database..."
    cp "$RESTORE_DIR/database.db" "./database.db"
    echo "   ✅ Database restored"
else
    echo "   ⚠️  No database found in backup"
fi

# Restore XML data
if [ -d "$RESTORE_DIR/data" ]; then
    echo "📥 Restoring XML files..."
    rm -rf "./data"
    mkdir -p "./data"
    cp -r "$RESTORE_DIR/data"/* "./data/" 2>/dev/null || true
    FILE_COUNT=$(ls -1 "./data" 2>/dev/null | wc -l)
    echo "   ✅ Restored $FILE_COUNT files"
else
    echo "   ⚠️  No XML data found in backup"
fi

# Cleanup
echo "🧹 Cleaning up..."
rm -rf "$RESTORE_DIR"

# Restart services
echo "🚀 Starting services..."
docker compose up -d

echo ""
echo "✅ Restore complete!"
echo "   Previous data backed up to: ./pre-restore-backup/"
echo "   Services restarted"
echo ""
echo "Verify the restoration:"
echo "  docker compose logs -f"
echo "  curl http://localhost:8000/stats"
