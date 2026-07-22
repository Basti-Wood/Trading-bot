#!/usr/bin/env bash
# dashboard/entrypoint.sh — Generate .htpasswd from DASHBOARD_PASSWORD and start supervisord
set -e

if [ -n "${DASHBOARD_PASSWORD}" ]; then
    echo "🔐 Setting up dashboard password..."
    # Use openssl to generate a bcrypt-compatible hash (apr1)
    HASH=$(openssl passwd -apr1 "${DASHBOARD_PASSWORD}")
    echo "admin:${HASH}" > /etc/nginx/.htpasswd
    echo "✅ Dashboard password configured."
else
    echo "⚠️  DASHBOARD_PASSWORD not set — dashboard has NO password protection!"
    # Create empty htpasswd so nginx doesn't crash
    touch /etc/nginx/.htpasswd
fi

exec supervisord -c /etc/supervisor/supervisord.conf
