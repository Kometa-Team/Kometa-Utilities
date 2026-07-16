# Path-Based Deployment Configuration

This guide explains how to deploy the AniDB Service at a path like `yourdomain.com/anidb-service` instead of a subdomain.

## When to Use Path-Based Routing

**Use path-based routing when:**
- You want to host multiple services on one domain
- Example: `yourdomain.com/anidb-service`, `yourdomain.com/plex-utils`, etc.
- You want to consolidate services under a single certificate

**Use subdomain routing when:**
- You want service isolation
- Example: `anidb.yourdomain.com`, `plex.yourdomain.com`
- Simpler configuration for single services

## Configuration Steps

### 1. Update Environment Variables

Edit your `.env` file:

```bash
# Add this line for path-based routing
ROOT_PATH=/anidb-service

# For subdomain routing, leave it empty
# ROOT_PATH=
```

### 2. Update Caddyfile

The Caddyfile is already configured for path-based routing. Just update the domain:

```caddy
yourdomain.com {
    handle_errors {
        @maintenance expression {err.status_code} in [502, 503, 504]
        handle @maintenance {
            rewrite * /maintenance.html?path={path}&host={host}
            file_server {
                root /var/www/html
            }
        }
    }

    handle /anidb-service* {
        reverse_proxy anidb-mirror:8000
    }
}
```

Replace `yourdomain.com` with your actual domain.

**Important:** Do NOT use `uri strip_prefix` - FastAPI handles the path prefix internally using ROOT_PATH.

### 3. Restart Services

```bash
docker compose down
docker compose up -d
```

## Testing

### Path-Based Access

```bash
# Stats endpoint
curl https://yourdomain.com/anidb-service/stats

# Get anime
curl https://yourdomain.com/anidb-service/anime/1

# API docs
https://yourdomain.com/anidb-service/docs
```

### Verify OpenAPI Docs

The FastAPI documentation should automatically adjust to the path:
- Swagger UI: `https://yourdomain.com/anidb-service/docs`
- ReDoc: `https://yourdomain.com/anidb-service/redoc`
- OpenAPI JSON: `https://yourdomain.com/anidb-service/openapi.json`

## Multiple Services Example

If you want to host multiple services on the same domain:

```caddy
yourdomain.com {
    # AniDB Service
    handle_path /anidb-service* {
        reverse_proxy anidb-mirror:8000
    }

    # Another service
    handle_path /plex-utils* {
        reverse_proxy plex-utils:3000
    }

    # Another service
    handle_path /kometa-api* {
        reverse_proxy kometa-api:5000
    }

    # Root path
    handle / {
        respond "Available services: /anidb-service, /plex-utils, /kometa-api"
    }
}
```

## Troubleshooting

### API Links are Broken

Make sure `ROOT_PATH` is set in your `.env` file:
```bash
ROOT_PATH=/anidb-service
```

Then restart:
```bash
docker compose restart anidb-mirror
```

### 404 Not Found

Check that:
1. The path in Caddyfile matches your URL
2. The `ROOT_PATH` in `.env` matches the Caddyfile path
3. You're accessing the correct URL: `yourdomain.com/anidb-service/stats` (not `anidb-service.yourdomain.com`)

### Swagger UI Not Loading

The `root_path` parameter in FastAPI automatically fixes the OpenAPI schema paths. If it's not working:

1. Check browser console for errors
2. Verify the `ROOT_PATH` environment variable is set
3. Check that the OpenAPI JSON is accessible: `curl https://yourdomain.com/anidb-service/openapi.json`

## Switching Between Modes

### From Subdomain to Path-Based

1. Update DNS to point domain (not subdomain) to server
2. Add `ROOT_PATH=/anidb-service` to `.env`
3. Update Caddyfile to use path-based routing
4. Restart services

### From Path-Based to Subdomain

1. Update DNS to use subdomain
2. Remove or empty `ROOT_PATH` in `.env`
3. Update Caddyfile to use subdomain:
   ```caddy
   anidb-service.yourdomain.com {
       reverse_proxy anidb-mirror:8000
   }
   ```
4. Restart services

## Best Practices

1. **Consistent Naming**: Use the same path prefix in both Caddyfile and ROOT_PATH
2. **Trailing Slashes**: The configuration handles both `/anidb-service` and `/anidb-service/`
3. **Testing**: Always test all endpoints after changing routing mode
4. **Documentation**: Document which services are at which paths
5. **Monitoring**: Update monitoring URLs if you switch modes

## Example Configurations

### Development (Subdomain)

**.env:**
```bash
ROOT_PATH=
```

**Caddyfile:**
```caddy
anidb-service.localhost {
    reverse_proxy anidb-mirror:8000
}
```

**Access:**
- http://anidb-service.localhost/stats

### Production (Path-Based)

**.env:**
```bash
ROOT_PATH=/anidb-service
```

**Caddyfile:**
```caddy
yourdomain.com {
    handle_path /anidb-service* {
        reverse_proxy anidb-mirror:8000
    }
}
```

**Access:**
- https://yourdomain.com/anidb-service/stats

## DNS Configuration

### For Path-Based Routing

Only need A record for the main domain:

```
Type: A
Name: @
Value: YOUR_SERVER_IP
TTL: 300
```

### For Subdomain Routing

Need A record for the subdomain:

```
Type: A
Name: anidb-service
Value: YOUR_SERVER_IP
TTL: 300
```

## Kometa Configuration

Update your Kometa config to use the correct URL:

**Path-based:**
```yaml
anidb:
  url: https://yourdomain.com/anidb-service
  username: your_username
  password: your_password
```

**Subdomain:**
```yaml
anidb:
  url: https://anidb-service.yourdomain.com
  username: your_username
  password: your_password
```
