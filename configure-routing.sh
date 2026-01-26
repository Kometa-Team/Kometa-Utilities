#!/bin/bash
# Configure routing mode for AniDB Service

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║      AniDB Service - Routing Configuration Helper         ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    echo "Run this from the AniDB-Service directory"
    exit 1
fi

echo "Choose routing mode:"
echo "1) Path-based routing (e.g., yourdomain.com/anidb-service)"
echo "2) Subdomain routing (e.g., anidb-service.yourdomain.com)"
echo ""
read -p "Enter choice [1-2]: " choice

case $choice in
    1)
        echo ""
        echo "📍 Configuring path-based routing..."
        read -p "Enter domain (e.g., yourdomain.com): " DOMAIN
        read -p "Enter path prefix (e.g., /anidb-service): " PATH_PREFIX
        
        # Remove leading slash if present
        PATH_PREFIX=${PATH_PREFIX#/}
        
        # Update .env
        if grep -q "^ROOT_PATH=" .env; then
            sed -i.bak "s|^ROOT_PATH=.*|ROOT_PATH=/${PATH_PREFIX}|" .env
        else
            echo "ROOT_PATH=/${PATH_PREFIX}" >> .env
        fi
        
        # Update Caddyfile
        cat > Caddyfile <<EOF
${DOMAIN} {
    handle /${PATH_PREFIX}* {
        uri strip_prefix /${PATH_PREFIX}
        
        handle_errors {
            @maintenance expression {err.status_code} in [502, 503, 504]
            handle @maintenance {
                rewrite * /maintenance.html
                file_server {
                    root /var/www/html
                }
            }
        }

        reverse_proxy anidb-mirror:8000
    }

    # Optional: Root path handler
    handle / {
        respond "Available services: /${PATH_PREFIX}"
    }
}
EOF
        
        echo ""
        echo "✅ Configuration updated!"
        echo ""
        echo "📋 Summary:"
        echo "   Mode: Path-based"
        echo "   URL: https://${DOMAIN}/${PATH_PREFIX}"
        echo "   Stats: https://${DOMAIN}/${PATH_PREFIX}/stats"
        echo "   Docs: https://${DOMAIN}/${PATH_PREFIX}/docs"
        echo ""
        echo "📝 Next steps:"
        echo "   1. Add DNS A record: ${DOMAIN} → YOUR_SERVER_IP"
        echo "   2. Restart services: docker compose restart"
        echo "   3. Test: curl https://${DOMAIN}/${PATH_PREFIX}/stats"
        ;;
        
    2)
        echo ""
        echo "🌐 Configuring subdomain routing..."
        read -p "Enter full subdomain (e.g., anidb-service.yourdomain.com): " SUBDOMAIN
        
        # Update .env
        if grep -q "^ROOT_PATH=" .env; then
            sed -i.bak "s|^ROOT_PATH=.*|ROOT_PATH=|" .env
        else
            echo "ROOT_PATH=" >> .env
        fi
        
        # Update Caddyfile
        cat > Caddyfile <<EOF
${SUBDOMAIN} {
    handle_errors {
        @maintenance expression {err.status_code} in [502, 503, 504]
        handle @maintenance {
            rewrite * /maintenance.html
            file_server {
                root /var/www/html
            }
        }
    }

    reverse_proxy anidb-mirror:8000
}
EOF
        
        echo ""
        echo "✅ Configuration updated!"
        echo ""
        echo "📋 Summary:"
        echo "   Mode: Subdomain"
        echo "   URL: https://${SUBDOMAIN}"
        echo "   Stats: https://${SUBDOMAIN}/stats"
        echo "   Docs: https://${SUBDOMAIN}/docs"
        echo ""
        echo "📝 Next steps:"
        echo "   1. Add DNS A record: ${SUBDOMAIN} → YOUR_SERVER_IP"
        echo "   2. Restart services: docker compose restart"
        echo "   3. Test: curl https://${SUBDOMAIN}/stats"
        ;;
        
    *)
        echo "❌ Invalid choice"
        exit 1
        ;;
esac

echo ""
read -p "Restart services now? (y/n): " RESTART
if [[ $RESTART =~ ^[Yy]$ ]]; then
    echo "🔄 Restarting services..."
    docker compose restart
    echo "✅ Services restarted"
    echo ""
    echo "Wait a few seconds, then test your configuration"
fi

echo ""
echo "📚 For more information, see: docs/PATH_BASED_ROUTING.md"
