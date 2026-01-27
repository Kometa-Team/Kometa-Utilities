# AWS Elastic Beanstalk Deployment Guide

This guide will help you deploy the Plex OAuth application to AWS Elastic Beanstalk using the free tier.

## Free Tier Eligibility

AWS Free Tier includes:
- 750 hours/month of t2.micro EC2 instance (enough for 1 instance running 24/7)
- 5 GB of storage
- Valid for 12 months from account creation

## Prerequisites

1. **AWS Account**: Create an account at [aws.amazon.com](https://aws.amazon.com)
2. **AWS CLI**: Install the AWS CLI
   ```bash
   # macOS
   brew install awscli
   
   # Linux
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
   unzip awscliv2.zip
   sudo ./aws/install
   
   # Windows
   # Download and run: https://awscli.amazonaws.com/AWSCLIV2.msi
   ```

3. **EB CLI**: Install the Elastic Beanstalk CLI
   ```bash
   pip install awsebcli
   ```

4. **Configure AWS Credentials**:
   ```bash
   aws configure
   ```
   Enter your:
   - AWS Access Key ID
   - AWS Secret Access Key
   - Default region (e.g., `us-east-1`)
   - Default output format: `json`

## Deployment Steps

### 1. Initialize Elastic Beanstalk

From your project directory:

```bash
eb init
```

Follow the prompts:
- Select your region
- Choose "Create new Application"
- Application name: `plex-oauth`
- Platform: `Python`
- Platform branch: `Python 3.11` (or latest available)
- CodeCommit: `No`
- SSH: `Yes` (recommended for debugging)

### 2. Create Environment

```bash
eb create plex-oauth-env --instance-type t2.micro --single
```

Options explained:
- `plex-oauth-env`: Environment name
- `--instance-type t2.micro`: Free tier eligible instance
- `--single`: Single instance (not load balanced, saves costs)

**If you get a "No default VPC" error**, see [VPC_SETUP.md](VPC_SETUP.md) for solutions. Quick fix:

```bash
aws ec2 create-default-vpc
```

Then retry the `eb create` command above.

### 3. Set Environment Variables

```bash
eb setenv SECRET_KEY="$(python -c 'import os; print(os.urandom(24).hex())')"
eb setenv APP_NAME="Kometa Plex Auth"
eb setenv APP_VERSION="1.0"
```

### 4. Deploy the Application

```bash
eb deploy
```

### 5. Open Your Application

```bash
eb open
```

This will open your application in the default browser.

## Managing Your Application

### View Application Status

```bash
eb status
```

### View Logs

```bash
eb logs
```

### SSH into Instance

```bash
eb ssh
```

### Update Application

After making code changes:

```bash
eb deploy
```

### Environment Variables

View current variables:
```bash
eb printenv
```

Set a variable:
```bash
eb setenv VARIABLE_NAME="value"
```

### Terminate Environment (Stop Charges)

When you want to stop the application:

```bash
eb terminate plex-oauth-env
```

**Note**: This will delete the environment. To restart, run `eb create` again.

## Monitoring and Costs

### Check Free Tier Usage

1. Go to [AWS Billing Console](https://console.aws.amazon.com/billing/)
2. Click "Free Tier" in the left sidebar
3. Monitor your usage to stay within limits

### Set Up Billing Alerts

1. Go to [AWS Budgets](https://console.aws.amazon.com/billing/home#/budgets)
2. Create a budget to alert you if charges exceed $1-5

## Custom Domain (Optional)

To use a custom domain:

1. In Elastic Beanstalk console, go to your environment
2. Configuration → Load balancer (if using) or custom domain
3. Or use Route 53 to point your domain to the EB URL

## Troubleshooting

### Application Not Starting

Check logs:
```bash
eb logs --all
```

### Environment Health Issues

View health:
```bash
eb health --refresh
```

### Common Issues

1. **No default VPC**: See [VPC_SETUP.md](VPC_SETUP.md) - Run `aws ec2 create-default-vpc`
2. **502 Bad Gateway**: Check application logs, usually a Python error
3. **504 Gateway Timeout**: Application taking too long to start
4. **Environment variables not set**: Run `eb setenv` commands again

## Configuration Files

The deployment uses these configuration files:

- `requirements.txt`: Python dependencies
- `application.py`: EB entry point (imports app from app.py)
- `.ebextensions/01_flask.config`: Flask/WSGI configuration
- `.ebextensions/02_python.config`: Python process configuration
- `.platform/nginx/conf.d/proxy.conf`: Nginx configuration

## Security Notes

- Never commit `.env` files or secrets to git
- Use `eb setenv` to set sensitive environment variables
- Enable HTTPS in production (requires load balancer or CloudFront)
- Regularly update dependencies: `pip list --outdated`

## Cost Optimization

- Use `--single` instance mode (no load balancer)
- Use t2.micro instance type
- Terminate environment when not in use
- Monitor free tier usage monthly

## Alternative: AWS Lambda (Serverless)

For even lower costs, consider AWS Lambda with API Gateway (more complex setup but potentially free forever):
- Lambda: 1M free requests/month
- API Gateway: 1M free requests/month

Would you like instructions for Lambda deployment instead?

## Support

- [AWS Free Tier FAQs](https://aws.amazon.com/free/free-tier-faqs/)
- [Elastic Beanstalk Documentation](https://docs.aws.amazon.com/elasticbeanstalk/)
- [EB CLI Reference](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/eb-cli3.html)
