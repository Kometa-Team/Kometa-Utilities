#!/bin/bash
set -e

# DigitalOcean Spaces Backup Script
# Backs up database and XML data to DigitalOcean Spaces (S3-compatible storage)

# Configuration
BACKUP_DIR="./backups"
DB_FILE="./database.db"
XML_DATA="./data"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
BACKUP_PATH="$BACKUP_DIR/$TIMESTAMP"

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

echo "🔄 Starting backup to Spaces for $TIMESTAMP..."

# Create backup directory
mkdir -p "$BACKUP_PATH"

# Backup database
if [ -f "$DB_FILE" ]; then
    echo "📦 Backing up database..."
    cp "$DB_FILE" "$BACKUP_PATH/database.db"
    DB_SIZE=$(du -h "$BACKUP_PATH/database.db" | cut -f1)
    echo "   Database: $DB_SIZE"
else
    echo "⚠️  Database file not found, skipping"
fi

# Backup XML data (only if directory exists and has files)
if [ -d "$XML_DATA" ] && [ "$(ls -A $XML_DATA)" ]; then
    echo "📦 Backing up XML files..."
    mkdir -p "$BACKUP_PATH/data"
    cp -r "$XML_DATA"/* "$BACKUP_PATH/data/" 2>/dev/null || true
    FILE_COUNT=$(ls -1 "$BACKUP_PATH/data" 2>/dev/null | wc -l)
    echo "   Backed up $FILE_COUNT files"
else
    echo "⚠️  No XML files to backup"
fi

# Create compressed archive
echo "🗜️  Creating compressed archive..."
ARCHIVE_NAME="anidb-backup-$TIMESTAMP.tar.gz"
tar -czf "$BACKUP_DIR/$ARCHIVE_NAME" -C "$BACKUP_PATH" .
rm -rf "$BACKUP_PATH"

# Calculate size
BACKUP_SIZE=$(du -h "$BACKUP_DIR/$ARCHIVE_NAME" | cut -f1)
echo "   Archive size: $BACKUP_SIZE"

# Upload to Spaces
echo "☁️  Uploading to DigitalOcean Spaces..."
aws s3 cp \
    $ENDPOINT_URL \
    "$BACKUP_DIR/$ARCHIVE_NAME" \
    "s3://$S3_BUCKET_NAME/$ARCHIVE_NAME" \
    --quiet

if [ $? -eq 0 ]; then
    echo "✅ Successfully uploaded to Spaces: s3://$S3_BUCKET_NAME/$ARCHIVE_NAME"
else
    echo "❌ Failed to upload to Spaces"
    exit 1
fi

# List recent backups in Spaces
echo ""
echo "📊 Recent backups in Spaces:"
aws s3 ls \
    $ENDPOINT_URL \
    "s3://$S3_BUCKET_NAME/" \
    --human-readable \
    --summarize \
    | grep "anidb-backup-" | tail -n 5

# Optional: Keep only last 7 local backups
echo ""
echo "🧹 Cleaning old local backups (keeping last 7)..."
ls -t "$BACKUP_DIR"/anidb-backup-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm
echo "   Local backups cleaned"

# Optional: Delete old backups from Spaces (older than 30 days)
echo ""
echo "🗑️  Checking for old backups in Spaces (older than 30 days)..."
CUTOFF_DATE=$(date -d '30 days ago' +%s 2>/dev/null || date -v-30d +%s)

aws s3 ls \
    $ENDPOINT_URL \
    "s3://$S3_BUCKET_NAME/" \
    | grep "anidb-backup-" \
    | while read -r line; do
        BACKUP_DATE=$(echo "$line" | awk '{print $1 " " $2}')
        BACKUP_FILE=$(echo "$line" | awk '{print $4}')
        
        # Convert backup date to timestamp
        BACKUP_TIMESTAMP=$(date -d "$BACKUP_DATE" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$BACKUP_DATE" +%s)
        
        if [ "$BACKUP_TIMESTAMP" -lt "$CUTOFF_DATE" ]; then
            echo "   Deleting old backup: $BACKUP_FILE"
            aws s3 rm $ENDPOINT_URL "s3://$S3_BUCKET_NAME/$BACKUP_FILE" --quiet
        fi
    done

echo ""
echo "✅ Backup process finished"
echo "   Local: $BACKUP_DIR/$ARCHIVE_NAME ($BACKUP_SIZE)"
echo "   Spaces: s3://$S3_BUCKET_NAME/$ARCHIVE_NAME"
