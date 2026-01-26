#!/bin/bash
set -e

# DigitalOcean Deployment Helper Script
# Simplifies initial deployment of AniDB Service on DigitalOcean

echo "╔════════════════════════════════════════════════════════════╗"
echo "║      AniDB Service - DigitalOcean Deployment Helper       ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_step() {
    echo -e "${BLUE}==>${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    print_error "Please don't run this script as root"
    print_warning "If you just created your droplet, switch to a non-root user first:"
    echo "  sudo adduser deploy"
    echo "  sudo usermod -aG sudo deploy"
    echo "  sudo usermod -aG docker deploy"
    exit 1
fi

# Check if we're on a fresh DigitalOcean droplet
if [ ! -f /etc/digitalocean ]; then
    print_warning "This script is designed for DigitalOcean droplets"
    read -p "Continue anyway? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""
print_step "Step 1: Checking prerequisites..."

# Check for Docker
if ! command -v docker &> /dev/null; then
    print_warning "Docker not found. Installing..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    print_success "Docker installed"
    print_warning "Please log out and log back in for Docker group changes to take effect"
    exit 0
else
    print_success "Docker found: $(docker --version)"
fi

# Check for Git
if ! command -v git &> /dev/null; then
    print_warning "Git not found. Installing..."
    sudo apt-get update
    sudo apt-get install -y git
    print_success "Git installed"
else
    print_success "Git found: $(git --version)"
fi

# Check for AWS CLI (for Spaces)
if ! command -v aws &> /dev/null; then
    print_warning "AWS CLI not found (needed for Spaces backups)"
    read -p "Install AWS CLI? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_step "Installing AWS CLI..."
        curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
        unzip -q awscliv2.zip
        sudo ./aws/install
        rm -rf awscliv2.zip aws/
        print_success "AWS CLI installed"
    fi
else
    print_success "AWS CLI found: $(aws --version)"
fi

echo ""
print_step "Step 2: Getting deployment information..."

# Ask for domain
read -p "Enter your domain name (e.g., anidb.yourdomain.com): " DOMAIN_NAME
if [ -z "$DOMAIN_NAME" ]; then
    print_error "Domain name is required"
    exit 1
fi

# Ask for API credentials
echo ""
print_step "Step 3: API Authentication Setup..."
read -p "Enter API username [kometa_admin]: " API_USER
API_USER=${API_USER:-kometa_admin}

read -s -p "Enter API password (will be hidden): " API_PASS
echo ""
if [ -z "$API_PASS" ]; then
    print_error "API password is required"
    exit 1
fi

# Ask for AniDB credentials
echo ""
print_step "Step 4: AniDB Credentials..."
print_warning "You need an AniDB account to access mature content"
read -p "Enter AniDB username: " ANIDB_USER
read -s -p "Enter AniDB password (will be hidden): " ANIDB_PASS
echo ""

# Ask about Spaces
echo ""
print_step "Step 5: DigitalOcean Spaces Setup (Optional)..."
print_warning "Spaces provides S3-compatible backup storage"
read -p "Configure DigitalOcean Spaces for backups? (y/n): " -n 1 -r
echo ""
SETUP_SPACES=false
if [[ $REPLY =~ ^[Yy]$ ]]; then
    SETUP_SPACES=true
    echo ""
    echo "Get your Spaces credentials from:"
    echo "https://cloud.digitalocean.com/account/api/spaces"
    echo ""
    read -p "Enter Spaces Access Key: " SPACES_KEY
    read -s -p "Enter Spaces Secret Key: " SPACES_SECRET
    echo ""
    read -p "Enter Spaces Region [nyc3]: " SPACES_REGION
    SPACES_REGION=${SPACES_REGION:-nyc3}
    read -p "Enter Spaces Bucket Name: " SPACES_BUCKET
fi

# Clone repository
echo ""
print_step "Step 6: Cloning repository..."
INSTALL_DIR="$HOME/anidb-service"

if [ -d "$INSTALL_DIR" ]; then
    print_warning "Directory $INSTALL_DIR already exists"
    read -p "Remove and re-clone? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_DIR"
    else
        cd "$INSTALL_DIR"
        git pull origin main
    fi
fi

if [ ! -d "$INSTALL_DIR" ]; then
    git clone https://github.com/Kometa-Team/AniDB-Service.git "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
print_success "Repository ready at $INSTALL_DIR"

# Create .env file
echo ""
print_step "Step 7: Creating .env configuration..."

cat > .env <<EOF
# AniDB Service Configuration
# Generated by deploy-digitalocean.sh on $(date)

# === API Authentication ===
API_USER=$API_USER
API_PASS=$API_PASS

# === AniDB API Configuration ===
ANIDB_CLIENT=kometaofficial
ANIDB_VERSION=1
ANIDB_PROTO_VER=1

# AniDB Authentication
ANIDB_USERNAME=$ANIDB_USER
ANIDB_PASSWORD=$ANIDB_PASS

# === Rate Limiting ===
DAILY_LIMIT=200
THROTTLE_SECONDS=4
UPDATE_THRESHOLD_DAYS=7

# === File Paths (Docker defaults) ===
XML_DIR=/app/data
DB_PATH=/app/database.db
SEED_DATA_DIR=/app/seed_data

EOF

if [ "$SETUP_SPACES" = true ]; then
    cat >> .env <<EOF
# === DigitalOcean Spaces Backup ===
AWS_ACCESS_KEY_ID=$SPACES_KEY
AWS_SECRET_ACCESS_KEY=$SPACES_SECRET
AWS_DEFAULT_REGION=$SPACES_REGION
S3_BUCKET_NAME=$SPACES_BUCKET
S3_ENDPOINT=https://${SPACES_REGION}.digitaloceanspaces.com

EOF
fi

print_success ".env file created"

# Update Caddyfile
echo ""
print_step "Step 8: Updating Caddyfile with domain..."

if [ ! -f Caddyfile ]; then
    cp Caddyfile.example Caddyfile
fi

cat > Caddyfile <<EOF
$DOMAIN_NAME {
    # Authentication is handled by FastAPI - no duplicate auth layer needed
    
    # Handle errors (like when the backend is down)
    handle_errors {
        @maintenance expression {err.status_code} in [502, 503, 504]
        handle @maintenance {
            rewrite * /maintenance.html
            file_server {
                root /var/www/html
            }
        }
    }

    # Proxy to FastAPI backend
    reverse_proxy anidb-mirror:8000
}
EOF

print_success "Caddyfile updated with $DOMAIN_NAME"

# Create necessary directories
echo ""
print_step "Step 9: Creating directories..."
mkdir -p data seed_data backups
print_success "Directories created"

# Make scripts executable
echo ""
print_step "Step 10: Setting up scripts..."
chmod +x backup.sh update.sh
if [ -f backup-spaces.sh ]; then
    chmod +x backup-spaces.sh
fi
if [ -f restore-spaces.sh ]; then
    chmod +x restore-spaces.sh
fi
print_success "Scripts are executable"

# Pull Docker images
echo ""
print_step "Step 11: Pulling Docker images..."
docker compose pull

# Start services
echo ""
print_step "Step 12: Starting services..."
docker compose up -d --build

echo ""
print_success "Waiting for services to start..."
sleep 5

# Check if services are running
if docker compose ps | grep -q "Up"; then
    print_success "Services are running!"
else
    print_error "Services failed to start. Check logs with: docker compose logs"
    exit 1
fi

# Test endpoint
echo ""
print_step "Step 13: Testing service..."
sleep 3

if curl -s http://localhost:8000/stats > /dev/null; then
    print_success "Service is responding!"
else
    print_warning "Service might not be ready yet. Check logs: docker compose logs -f"
fi

# Setup cron jobs
echo ""
print_step "Step 14: Setting up automated tasks..."
read -p "Setup automated backups and updates? (y/n): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    CRON_BACKUP="0 2 * * * cd $INSTALL_DIR && ./backup.sh >> $HOME/backup.log 2>&1"
    CRON_UPDATE="0 3 * * * cd $INSTALL_DIR && ./update.sh >> $HOME/update.log 2>&1"
    CRON_RESTART="0 4 * * 0 cd $INSTALL_DIR && docker compose restart >> $HOME/restart.log 2>&1"
    
    if [ "$SETUP_SPACES" = true ]; then
        CRON_SPACES="0 2 * * * cd $INSTALL_DIR && ./backup-spaces.sh >> $HOME/backup-spaces.log 2>&1"
        (crontab -l 2>/dev/null; echo "$CRON_SPACES") | crontab -
    else
        (crontab -l 2>/dev/null; echo "$CRON_BACKUP") | crontab -
    fi
    
    (crontab -l 2>/dev/null; echo "$CRON_UPDATE") | crontab -
    (crontab -l 2>/dev/null; echo "$CRON_RESTART") | crontab -
    
    print_success "Automated tasks scheduled"
    echo "  • Daily backups at 2 AM"
    echo "  • Daily updates at 3 AM"
    echo "  • Weekly restart on Sunday at 4 AM"
fi

# Final instructions
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║                 Deployment Complete! 🎉                    ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
print_success "AniDB Service is now running!"
echo ""
echo "📝 Next Steps:"
echo ""
echo "1. Configure DNS:"
echo "   Add an A record pointing to this server's IP:"
echo "   Type: A"
echo "   Name: $(echo $DOMAIN_NAME | cut -d. -f1)"
echo "   Value: $(curl -s ifconfig.me)"
echo "   TTL: 300"
echo ""
echo "2. Wait for DNS propagation (5-30 minutes)"
echo "   Check with: nslookup $DOMAIN_NAME"
echo ""
echo "3. Test your service:"
echo "   Local:  curl http://localhost:8000/stats"
echo "   Public: curl https://$DOMAIN_NAME/stats"
echo "   Auth:   curl -u $API_USER:*** https://$DOMAIN_NAME/anime/1"
echo ""
echo "4. Monitor logs:"
echo "   docker compose logs -f"
echo ""
echo "5. View service status:"
echo "   docker compose ps"
echo ""
echo "📁 Installation directory: $INSTALL_DIR"
echo "🔧 Configuration file: $INSTALL_DIR/.env"
echo "🌐 Domain: $DOMAIN_NAME"
echo ""
if [ "$SETUP_SPACES" = true ]; then
    echo "☁️  Spaces backup: Configured"
    echo "   Bucket: $SPACES_BUCKET"
    echo "   Region: $SPACES_REGION"
    echo ""
fi
echo "📚 Documentation:"
echo "   • Quick Start: $INSTALL_DIR/QUICKSTART.md"
echo "   • DigitalOcean Guide: $INSTALL_DIR/DIGITALOCEAN_DEPLOYMENT.md"
echo "   • Testing Guide: $INSTALL_DIR/TESTING.md"
echo ""
print_success "Setup complete! Your AniDB Mirror Service is ready."
