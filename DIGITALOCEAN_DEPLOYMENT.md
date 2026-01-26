# DigitalOcean Deployment Guide

Deploy AniDB Mirror Service on DigitalOcean - Simple, fast, and affordable at **$6/month**.

**Choose Your Deployment Method:**
- 🚀 **[Quick Deploy](#quick-deploy-script)** - Automated script (recommended for beginners)
- ⚙️ **[Terraform Deploy](#terraform-deployment)** - Infrastructure as Code (recommended for production)
- 📖 **[Manual Deploy](#manual-deployment)** - Step-by-step guide (for learning)

---

## 💰 What You'll Get

**Basic Droplet ($6/month):**
- **1 GB RAM**
- **1 vCPU**
- **25 GB SSD**
- **1 TB transfer**
- **Free backups:** +20% ($1.20/month)

**New Account Bonus:**
- **$200 credit** free for 60 days
- Test for 2 months completely free!

**Annual Cost:** $72/year ($60/year if paid annually - save $12)

---

## 🚀 Quick Deploy (Script)

The fastest way to get started! This automated script handles everything for you.

### Prerequisites

- A DigitalOcean droplet running Ubuntu 24.04
- SSH access to the droplet
- A domain name pointing to your droplet

### Deploy in 2 Minutes

```bash
# SSH into your DigitalOcean droplet
ssh root@YOUR_DROPLET_IP

# Create non-root user (if not already done)
adduser deploy
usermod -aG sudo deploy
exit

# SSH as deploy user
ssh deploy@YOUR_DROPLET_IP

# Download and run deployment script
curl -sSL https://raw.githubusercontent.com/Kometa-Team/AniDB-Service/main/deploy-digitalocean.sh -o deploy.sh
chmod +x deploy.sh
./deploy.sh
```

The script will:
- ✅ Check and install prerequisites (Docker, Git, AWS CLI)
- ✅ Clone the repository
- ✅ Configure your domain and credentials
- ✅ Set up DigitalOcean Spaces backups (optional)
- ✅ Create and configure all necessary files
- ✅ Start the services
- ✅ Set up automated backups and updates

**That's it!** Your service will be running at `https://yourdomain.com`

---

## ⚙️ Terraform Deployment

For production deployments, use Terraform to automate infrastructure provisioning.

### What Terraform Creates

- Droplet with Ubuntu 24.04
- Firewall rules (SSH, HTTP, HTTPS)
- Reserved IP (optional)
- DigitalOcean Spaces bucket for backups
- DNS records (optional)
- Automated setup via user-data script

### Quick Terraform Deployment

```bash
# Install Terraform (if not installed)
brew install terraform  # macOS
# or: sudo apt install terraform  # Linux

# Clone repository locally
git clone https://github.com/Kometa-Team/AniDB-Service.git
cd AniDB-Service/terraform/digitalocean

# Configure your settings
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars

# Deploy!
terraform init
terraform apply
```

See [terraform/digitalocean/README.md](terraform/digitalocean/README.md) for detailed instructions.

### Terraform Benefits

- 🎯 **Reproducible** - Deploy identical infrastructure every time
- 📦 **Complete** - All resources created with one command
- 🔄 **Version Controlled** - Track infrastructure changes in git
- 🗑️ **Easy Cleanup** - Destroy everything with `terraform destroy`
- 📊 **Cost Tracking** - See estimated costs before deploying

---

## 📖 Manual Deployment

Follow these steps for a hands-on deployment experience.

### Part 1: DigitalOcean Account Setup

#### 1.1 Create Account

1. Go to [digitalocean.com](https://www.digitalocean.com)
2. Click **Sign Up**
3. Use GitHub/Google or email signup
4. Verify email address

#### 1.2 Add Payment Method

1. Go to **Account** → **Billing**
2. Add credit card or PayPal
3. New accounts get **$200 credit** for 60 days

#### 1.3 Set Up Billing Alerts

1. Go to **Account** → **Billing** → **Settings**
2. Enable **Email Billing Alerts**
3. Set threshold: $5
4. Save preferences

---

### Part 2: Create Droplet

#### 2.1 Create New Droplet

1. Click **Create** → **Droplets**
2. Or go directly to [cloud.digitalocean.com/droplets/new](https://cloud.digitalocean.com/droplets/new)

#### 2.2 Choose Configuration

**Region:**
- Select closest to your users
- Recommended: **New York 1** (US East) or **Frankfurt 1** (EU)
- Check latency: [cloudping.info](https://cloudping.info/)

**Image:**
- Choose **Ubuntu**
- Select **24.04 (LTS) x64**

**Droplet Type:**
- Select **Basic** (not Premium)

**CPU Options:**
- Select **Regular** (not Premium Intel or AMD)

**Size:**
- Select **$6/month**
  - 1 GB RAM / 1 vCPU
  - 25 GB SSD
  - 1 TB transfer

#### 2.3 Authentication

**SSH Keys (Recommended):**
1. Click **New SSH Key**
2. If you don't have one, generate on your machine:
   ```bash
   ssh-keygen -t ed25519 -C "anidb-digitalocean"
   # Save to: ~/.ssh/anidb_do
   ```
3. Copy public key:
   ```bash
   cat ~/.ssh/anidb_do.pub
   ```
4. Paste into DigitalOcean
5. Name it: `anidb-key`
6. Click **Add SSH Key**

**Or use Password** (less secure):
- You'll receive root password via email

#### 2.4 Additional Options

**Backups:**
- ✅ Enable automated backups (+$1.20/month)
- Weekly backups kept for 4 weeks

**Monitoring:**
- ✅ Enable (free) - CPU, bandwidth, disk metrics

**IPv6:**
- ✅ Enable (free) - optional but recommended

**Hostname:**
- Enter: `anidb-mirror`

**Tags:**
- Add: `production` (optional, for organization)

#### 2.5 Create Droplet

1. Review selections
2. Click **Create Droplet**
3. Wait 30-60 seconds for provisioning
4. Note your **Public IPv4 Address**

---

### Part 3: Initial Server Setup

#### 3.1 Connect via SSH

```bash
# If using SSH key
ssh root@[YOUR_DROPLET_IP]

# If using password
ssh root@[YOUR_DROPLET_IP]
# Enter password from email
```

#### 3.2 Update System

```bash
# Update packages
apt update && apt upgrade -y

# Install essentials
apt install -y git curl wget unzip ufw
```

#### 3.3 Create Non-Root User (Security Best Practice)

```bash
# Create user
adduser deploy
# Enter password when prompted

# Add to sudo group
usermod -aG sudo deploy

# Copy SSH keys (if using SSH)
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

#### 3.4 Configure Firewall

```bash
# Configure UFW firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# Verify
ufw status
```

#### 3.5 Switch to Deploy User

```bash
# Exit root session
exit

# Connect as deploy user
ssh deploy@[YOUR_DROPLET_IP]
```

---

### Part 4: Install Docker

#### 4.1 Install Docker

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add user to docker group
sudo usermod -aG docker $USER

# Exit and reconnect for group changes
exit
ssh deploy@[YOUR_DROPLET_IP]

# Verify
docker --version
docker compose version
```

---

### Part 5: Configure Domain

#### 5.1 Update DNS Records

1. Go to your domain registrar (or use DigitalOcean DNS)
2. Add A record:

```
Type: A
Name: anidb-service
Value: [YOUR_DROPLET_IP]
TTL: 3600
```

#### 5.2 (Optional) Use DigitalOcean DNS

1. Go to **Networking** → **Domains**
2. Enter your domain → **Add Domain**
3. Create DNS records:
   - **A Record:** `@` → `[DROPLET_IP]`
   - **A Record:** `anidb-service` → `[DROPLET_IP]`
4. Update nameservers at registrar:
   - `ns1.digitalocean.com`
   - `ns2.digitalocean.com`
   - `ns3.digitalocean.com`

#### 5.3 Verify DNS

```bash
# Check DNS propagation
nslookup anidb-service.yourdomain.com

# Or use
dig anidb-service.yourdomain.com
```

---

### Part 6: Deploy Application

#### 6.1 Clone Repository

```bash
cd ~
git clone https://github.com/Kometa-Team/AniDB-Service.git
cd AniDB-Service
```

#### 6.2 Configure Environment

```bash
# Copy template
cp .env.example .env

# Edit configuration
nano .env
```

**Update these values:**

```bash
# === API Authentication ===
API_USER=kometa_admin
API_PASS=YOUR_SECURE_PASSWORD_HERE

# === AniDB Configuration ===
ANIDB_USERNAME=your_anidb_username
ANIDB_PASSWORD=your_anidb_password

# === DigitalOcean Spaces (S3-compatible) ===
# Get keys from: https://cloud.digitalocean.com/account/api/spaces
AWS_ACCESS_KEY_ID=your_spaces_access_key
AWS_SECRET_ACCESS_KEY=your_spaces_secret_key
AWS_DEFAULT_REGION=nyc3  # Or your region
S3_BUCKET_NAME=anidb-backups
S3_ENDPOINT=https://nyc3.digitaloceanspaces.com
```

#### 6.3 Update Caddyfile

```bash
# Copy example if needed
if [ ! -f Caddyfile ]; then
    cp Caddyfile.example Caddyfile
fi

# Edit configuration
nano Caddyfile
```

Replace domain:
```
anidb-service.yourdomain.com {
    # ... rest of config
}
```

Or for path-based routing:
```
yourdomain.com {
    handle /anidb-service* {
        # ... rest of config
    }
}
```

#### 6.4 Upload Seed Data (Optional)

```bash
# On local machine
scp seed-data.zip deploy@[DROPLET_IP]:~/AniDB-Service/seed_data/
```

---

### Part 8: Launch Service

#### 8.1 Start Docker Containers

```bash
cd ~/AniDB-Service

# Build and start
docker compose up -d --build

# Watch logs
docker compose logs -f
```

#### 8.2 Verify Service

```bash
# Local test
curl http://localhost:8000/stats

# External test
curl https://anidb-service.yourdomain.com/stats

# Test authentication
curl -u kometa_admin:YOUR_PASSWORD https://anidb-service.yourdomain.com/anime/1
```

---

### Part Local Backups

```bash
# Make executable
chmod +x backup.sh update.sh

# Test local backup
./backup.sh

# Verify backup created
ls -lh backups/
```

#### 9.2 DigitalOcean Spaces Backups (Recommended)

For off-site backups to DigitalOcean Spaces (S3-compatible):

```bash
# Make script executable
chmod +x backup-spaces.sh restore-spaces.sh

# Test Spaces backup
./backup-spaces.sh

# Verify upload
# Check in DigitalOcean Console → Spaces
```

**Spaces Setup:**
1. Go to [DigitalOcean Spaces](https://cloud.digitalocean.com/spaces)
2. Click **Create a Spaces Bucket**
3. Choose region (same as your droplet)
4. Name: `anidb-backups-yourname`
5. Set to Private
6. Go to [API → Spaces Keys](https://cloud.digitalocean.com/account/api/spaces)
7. Click **Generate New Key**
8. Save Access Key and Secret Key
9. Add to `.env` file

#### 9.3 Schedule Automated Backups

```bash
# Edit crontab
crontab -e

# Add for Spaces backups (recommended):
# Daily backup to Spaces at 2 AM
0 2 * * * cd /home/deploy/AniDB-Service && ./backup-spaces.sh >> /home/deploy/backup.log 2>&1

# OR for local backups only:
# 0 2 * * * cd /home/deploy/AniDB-Service && ./backup.sh >> /home/deploy/backup.log 2>&1

# Daily update at 3 AM
0 3 * * * cd /home/deploy/AniDB-Service && ./update.sh >> /home/deploy/update.log 2>&1

# Weekly restart (clear memory)
0 4 * * 0 cd /home/deploy/AniDB-Service && docker compose restart
```

#### 9.4 Restore from Spaces Backup

```bash
# View available backups and restore
./restore-spaces.sh

# Follow prompts to select and restore a backup
# Weekly restart (clear memory)
0 4 * * 0 cd /home/deploy/AniDB-Service && docker compose restart
```

---

## 📊 Monitoring with DigitalOcean

### Built-in Monitoring (Free)

1. Go to **Droplets** → Select your droplet
2. Click **Graphs** tab
3. View metrics:
   - CPU usage
   - Bandwidth
   - Disk I/O
   - Memory (if monitoring enabled)

### Set Up Alerts

1. Go to **Manage** → **Monitoring** → **Alerts**
2. Click **Create Alert Policy**
3. Configure alerts:
   - **CPU > 90%** for 5 minutes
   - **Disk usage > 80%**
   - **Bandwidth > 80% of limit**
4. Add notification: Your email

### Enhanced Monitoring

```bash
# Install monitoring agent
curl -sSL https://repos.insights.digitalocean.com/install.sh | sudo bash
```

---

## 🔒 Security Hardening

### Disable Root Login

```bash
sudo nano /etc/ssh/sshd_config

# Set:
PermitRootLogin no
PasswordAuthentication no  # If using SSH keys only

# Restart SSH
sudo systemctl restart sshd
```

### Install Fail2Ban

```bash
# Install
sudo apt install -y fail2ban

# Configure
sudo cp /etc/fail2ban/jail.conf /etc/fail2ban/jail.local
sudo nano /etc/fail2ban/jail.local

# Update [sshd] section:
enabled = true
maxretry = 3
bantime = 3600

# Start service
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### Enable Automatic Security Updates

```bash
# Install unattended-upgrades
sudo apt install -y unattended-upgrades

# Enable
sudo dpkg-reconfigure -plow unattended-upgrades

# Configure
sudo nano /etc/apt/apt.conf.d/50unattended-upgrades
# Ensure these are uncommented:
# "${distro_id}:${distro_codename}-security";
```

---

## 💡 DigitalOcean Tips

### Resize Droplet (If Needed)

1. **Power off droplet first**
2. Go to droplet → **Resize**
3. Select larger size
4. Can only resize up, not down (unless flex resize)
5. Click **Resize**

### Snapshots vs Backups

**Automated Backups** ($1.20/month):
- Weekly, automatic
- 4 most recent kept
- Can't be turned off without losing backups

**Snapshots** (pay per GB):
- Manual, on-demand
- Keep forever until deleted
- ~$0.06/GB/month
- Use for major changes

### One-Click Apps (Alternative Deployment)

DigitalOcean has Docker one-click apps:
1. When creating droplet
2. Select **Marketplace**
3. Choose **Docker**
4. Pre-configured Docker environment

### Floating IP (Optional)

Like AWS Elastic IP:
1. Go to **Networking** → **Floating IPs**
2. **Create Floating IP**
3. Assign to your droplet
4. Free while assigned
5. Keeps IP if you rebuild droplet

---

## 🐛 Troubleshooting

### Service Won't Start

```bash
# Check logs
docker compose logs

# Check disk space
df -h

# Check memory
free -h

# If out of memory, add swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Can't Connect via SSH

```bash
# From DO Console
# Go to droplet → Access → Launch Console
# This gives you browser-based console access

# Check UFW status
sudo ufw status

# Check SSH service
sudo systemctl status sshd
```

### High CPU/Memory Usage

```bash
# Check Docker resource usage
docker stats

# View processes
htop  # Install: sudo apt install htop

# Restart services
docker compose restart
```

### Backup to Spaces Failing

```bash
# Test Spaces connection
aws s3 ls --endpoint-url=https://nyc3.digitaloceanspaces.com

# Check credentials in .env
cat .env | grep AWS_

# Test with small file
echo "test" > test.txt
aws s3 cp test.txt s3://anidb-backups/test.txt --endpoint-url=https://nyc3.digitaloceanspaces.com

# Verify
aws s3 ls s3://anidb-backups/ --endpoint-url=https://nyc3.digitaloceanspaces.com
```

---

## 💰 Cost Management

### Monthly Cost Breakdown

| Item | Cost |
|------|------|
| Basic Droplet | $6.00 |
| Automated Backups | $1.20 |
| **Total** | **$7.20/month** |

### Reduce Costs

1. **Skip automated backups** - Use manual snapshots
2. **Use local backups** - SSH and copy files periodically
3. **Annual billing** - Save 8% ($69.36 vs $72)
4. **Referral credits** - Get $200 for each referral

### Free $200 Credit

New accounts get $200 for 60 days:
- 2 months completely free
- Test before committing
- No credit card charge until credit depleted

---

## 📊 Performance Optimization

### Enable Swap (Recommended for 1GB RAM)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Optimize Docker

```bash
# Limit memory usage in docker-compose.yml
services:
  anidb-mirror:
    deploy:
      resources:
        limits:
          memory: 512M
        reservations:
          memory: 256M
```

### Caching

```bash
# Docker image cleanup
docker system prune -af --volumes

# Keep only recent backups
# Add to backup.sh:
# Delete backups older than 30 days
aws s3 ls s3://anidb-backups/ | grep -v "$(date +%Y-%m)" | awk '{print $4}' | xargs -I {} aws s3 rm s3://anidb-backups/{} --recursive
```

---

## ✅ Post-Deployment Checklist

- [ ] Droplet running with static IP
- [ ] Non-root user created
- [ ] SSH key authentication working
- [ ] Firewall (UFW) configured
- [ ] Domain DNS pointing to droplet
- [ ] HTTPS working via Caddy
- [ ] `/stats` endpoint accessible
- [ ] Authentication working
- [ ] DigitalOcean Spaces configured (optional)
- [ ] Backups scheduled and tested
- [ ] Monitoring alerts configured
- [ ] Fail2Ban installed
- [ ] Automatic updates enabled

---

## 📦 Available Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `deploy-digitalocean.sh` | Automated deployment | `./deploy-digitalocean.sh` |
| `backup.sh` | Local backup | `./backup.sh` |
| `backup-spaces.sh` | Backup to Spaces | `./backup-spaces.sh` |
| `restore-spaces.sh` | Restore from Spaces | `./restore-spaces.sh` |
| `update.sh` | Update service | `./update.sh` |

---

## 🏗️ Infrastructure as Code

For production deployments, use Terraform:

```bash
cd terraform/digitalocean
terraform init
terraform apply
```

See [terraform/digitalocean/README.md](terraform/digitalocean/README.md) for details.

---

## 🎉 Why DigitalOcean?

✅ **Simple pricing** - No surprise charges  
✅ **Excellent docs** - Best in the industry  
✅ **Fast SSDs** - Better than AWS EBS at this price  
✅ **Spaces** - S3-compatible storage included  
✅ **Free monitoring** - CPU, bandwidth, disk  
✅ **Great community** - Massive tutorial library  
✅ **Fast support** - Average response < 30 min  
✅ **$200 trial** - Test for 2 months free

**Perfect balance of simplicity, performance, and price for this service!**
