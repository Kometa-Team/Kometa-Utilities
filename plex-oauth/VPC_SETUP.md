# VPC Configuration for Elastic Beanstalk

## Problem: No Default VPC

If you see the error "No default VPC for this user", you have two solutions:

## Solution 1: Create a Default VPC (Recommended - Easiest)

Run this AWS CLI command to create a default VPC:

```bash
aws ec2 create-default-vpc
```

Then try the deployment again:

```bash
eb create plex-oauth-env --instance-type t2.micro --single
```

## Solution 2: Use a Custom VPC

If you want to use an existing VPC or create a custom one:

### Step 1: Find Your VPC and Subnet IDs

```bash
# List your VPCs
aws ec2 describe-vpcs --query 'Vpcs[*].[VpcId,CidrBlock,IsDefault]' --output table

# List subnets in your VPC (replace vpc-xxxxx with your VPC ID)
aws ec2 describe-subnets --filters "Name=vpc-id,Values=vpc-xxxxx" --query 'Subnets[*].[SubnetId,AvailabilityZone,CidrBlock]' --output table
```

### Step 2: Create EB Configuration File

Create a file `.elasticbeanstalk/config.yml` with your VPC details (this will be created automatically, but you can edit it):

```yaml
global:
  application_name: plex-oauth
  default_region: us-east-1
  
environment-defaults:
  plex-oauth-env:
    branch: null
    repository: null
```

### Step 3: Create Environment with VPC

```bash
eb create plex-oauth-env \
  --instance-type t2.micro \
  --single \
  --vpc.id vpc-xxxxx \
  --vpc.elbsubnets subnet-xxxxx \
  --vpc.ec2subnets subnet-xxxxx \
  --vpc.publicip
```

Replace:
- `vpc-xxxxx` with your VPC ID
- `subnet-xxxxx` with your subnet ID(s)

**Note**: Use the same subnet for both `elbsubnets` and `ec2subnets` for a single instance setup.

## Solution 3: Create New VPC (Advanced)

If you don't have any VPC:

```bash
# Create VPC
aws ec2 create-vpc --cidr-block 10.0.0.0/16

# Enable DNS hostname
aws ec2 modify-vpc-attribute --vpc-id vpc-xxxxx --enable-dns-hostnames

# Create subnet
aws ec2 create-subnet --vpc-id vpc-xxxxx --cidr-block 10.0.1.0/24 --availability-zone us-east-1a

# Create internet gateway
aws ec2 create-internet-gateway

# Attach to VPC
aws ec2 attach-internet-gateway --vpc-id vpc-xxxxx --internet-gateway-id igw-xxxxx

# Create route table and add route
aws ec2 create-route-table --vpc-id vpc-xxxxx
aws ec2 create-route --route-table-id rtb-xxxxx --destination-cidr-block 0.0.0.0/0 --gateway-id igw-xxxxx

# Associate route table with subnet
aws ec2 associate-route-table --route-table-id rtb-xxxxx --subnet-id subnet-xxxxx
```

Then use Solution 2 to create the EB environment.

## Verifying Your Setup

After creating default VPC, verify it exists:

```bash
aws ec2 describe-vpcs --filters "Name=isDefault,Values=true"
```

You should see one VPC marked as default.

## Try Again

Once you've created the default VPC (Solution 1), run:

```bash
eb create plex-oauth-env --instance-type t2.micro --single
```

## Still Having Issues?

Check the AWS Console:
1. Go to [VPC Console](https://console.aws.amazon.com/vpc/)
2. Verify you have at least one VPC
3. Note the VPC ID and subnet IDs
4. Use Solution 2 with those values
