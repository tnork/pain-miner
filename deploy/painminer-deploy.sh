#!/bin/bash
# One-step deploy for Pain Miner (standalone repo).
# Idempotent — safe to re-run after pulling new commits.
#
# Run as root on the server:
#   bash /opt/pain-miner/deploy/painminer-deploy.sh
#
# Credentials: reads PAINMINER_USER / PAINMINER_PASS from .env.
# Generate a random password if PAINMINER_PASS is missing.
#
# Prerequisites:
#   1. /opt/pain-miner/.env contains:
#        SUPABASE_URL, SUPABASE_ANON_KEY, PAINMINER_RPC_SECRET
#   2. Cloudflare Origin CA cert at /etc/ssl/cloudflare/origin.{crt,key}
#      (Cloudflare → SSL/TLS → Origin Server → Create Certificate, 15-year, *.muisbien.com)
#      OR a Let's Encrypt cert — update ssl_certificate paths in painminer-nginx.conf.
#   3. Cloudflare DNS A record painminer.muisbien.com → server IP, Proxy ON, SSL/TLS Full (strict)
#   4. schema/pain_miner.sql applied in Supabase + secret seeded into pain_miner_secrets

set -euo pipefail

REPO_DIR="/opt/pain-miner"
SITE_DIR="/var/www/painminer"
NGINX_AVAILABLE="/etc/nginx/sites-available/painminer"
NGINX_ENABLED="/etc/nginx/sites-enabled/painminer"
HTPASSWD_FILE="/etc/nginx/painminer.htpasswd"

if [[ ! -f "$REPO_DIR/.env" ]]; then
    echo "ERROR: $REPO_DIR/.env not found." >&2
    exit 1
fi

# Pull required vars from .env (line-by-line grep avoids shell metachars in other vars).
SUPABASE_URL=$(grep -E '^SUPABASE_URL=' "$REPO_DIR/.env" | head -1 | cut -d= -f2-)
SUPABASE_ANON_KEY=$(grep -E '^SUPABASE_ANON_KEY=' "$REPO_DIR/.env" | head -1 | cut -d= -f2-)
PAINMINER_RPC_SECRET=$(grep -E '^PAINMINER_RPC_SECRET=' "$REPO_DIR/.env" | head -1 | cut -d= -f2-)

_ENV_USER=$(grep -E '^PAINMINER_USER=' "$REPO_DIR/.env" | head -1 | cut -d= -f2-)
_ENV_PASS=$(grep -E '^PAINMINER_PASS=' "$REPO_DIR/.env" | head -1 | cut -d= -f2-)
PAINMINER_USER="${PAINMINER_USER:-${_ENV_USER:-painminer}}"
if [[ -z "${PAINMINER_PASS:-}" && -n "${_ENV_PASS:-}" ]]; then
    PAINMINER_PASS="$_ENV_PASS"
    PAINMINER_PASS_GENERATED=0
elif [[ -z "${PAINMINER_PASS:-}" ]]; then
    PAINMINER_PASS=$(openssl rand -hex 8)
    PAINMINER_PASS_GENERATED=1
else
    PAINMINER_PASS_GENERATED=0
fi

if [[ -z "${SUPABASE_URL:-}" || -z "${SUPABASE_ANON_KEY:-}" ]]; then
    echo "ERROR: SUPABASE_URL or SUPABASE_ANON_KEY missing from $REPO_DIR/.env." >&2
    exit 1
fi

if [[ -z "${PAINMINER_RPC_SECRET:-}" ]]; then
    echo "ERROR: PAINMINER_RPC_SECRET missing from $REPO_DIR/.env." >&2
    echo "Generate: openssl rand -hex 32" >&2
    echo "Then seed into Supabase: INSERT INTO pain_miner_secrets (key, value)" >&2
    echo "  VALUES ('rpc_shared_secret', '<value>');" >&2
    exit 1
fi

echo "==> Installing nginx + apache2-utils (htpasswd)"
apt-get update -qq
apt-get install -y -qq nginx apache2-utils

if [[ ! -f /etc/ssl/cloudflare/origin.crt || ! -f /etc/ssl/cloudflare/origin.key ]]; then
    echo ""
    echo "WARNING: Cloudflare Origin CA cert not found at /etc/ssl/cloudflare/origin.*"
    echo "  nginx -t will fail until installed. Steps:"
    echo "  1. Cloudflare → SSL/TLS → Origin Server → Create Certificate (15-year, *.muisbien.com)"
    echo "  2. mkdir -p /etc/ssl/cloudflare"
    echo "  3. Paste cert  → /etc/ssl/cloudflare/origin.crt"
    echo "  4. Paste key   → /etc/ssl/cloudflare/origin.key && chmod 640 origin.key"
    echo "  5. Re-run this script"
    echo ""
fi
mkdir -p /etc/ssl/cloudflare

echo "==> Creating site dir $SITE_DIR"
mkdir -p "$SITE_DIR"

# HTML pages
cp "$REPO_DIR/web/index.html"    "$SITE_DIR/index.html"
cp "$REPO_DIR/web/login.html"    "$SITE_DIR/login.html"
cp "$REPO_DIR/web/reports.html"  "$SITE_DIR/reports.html"
cp "$REPO_DIR/web/overview.html" "$SITE_DIR/overview.html"

# JS + CSS
cp "$REPO_DIR/web/error-shim.js" "$SITE_DIR/error-shim.js"
cp "$REPO_DIR/web/theme.css"     "$SITE_DIR/theme.css"

# Favicons
cp "$REPO_DIR/web/favicon.ico"    "$SITE_DIR/favicon.ico"
cp "$REPO_DIR/web/favicon-32.png" "$SITE_DIR/favicon-32.png"

# Brand images
mkdir -p "$SITE_DIR/images"
cp "$REPO_DIR/web/images/pain-miner-icon-cropped.png"  "$SITE_DIR/images/pain-miner-icon-cropped.png"
cp "$REPO_DIR/web/images/pain-miner-login-logo2.png"   "$SITE_DIR/images/pain-miner-login-logo2.png"
cp "$REPO_DIR/web/images/pain-miner-logo-cropped.png"  "$SITE_DIR/images/pain-miner-logo-cropped.png" 2>/dev/null || true

echo "==> Rendering config.js"
cat > "$SITE_DIR/config.js" <<EOF
window.PAINMINER_CONFIG = {
  supabaseUrl: "${SUPABASE_URL}",
  supabaseAnonKey: "${SUPABASE_ANON_KEY}",
  rpcSecret: "${PAINMINER_RPC_SECRET}",
};
EOF

chown -R www-data:www-data "$SITE_DIR"
chmod 644 "$SITE_DIR"/*.html "$SITE_DIR"/*.js "$SITE_DIR"/*.css 2>/dev/null || true
chmod 644 "$SITE_DIR"/images/*.png 2>/dev/null || true
chmod 640 "$SITE_DIR/config.js"   # RPC secret — nginx (www-data) only

echo "==> Creating htpasswd"
if [[ ! -f "$HTPASSWD_FILE" ]]; then
    htpasswd -bc "$HTPASSWD_FILE" "$PAINMINER_USER" "$PAINMINER_PASS"
else
    htpasswd -b "$HTPASSWD_FILE" "$PAINMINER_USER" "$PAINMINER_PASS"
fi
chown root:www-data "$HTPASSWD_FILE"
chmod 640 "$HTPASSWD_FILE"

echo "==> Installing nginx server block"
cp "$REPO_DIR/deploy/painminer-nginx.conf" "$NGINX_AVAILABLE"
ln -sf "$NGINX_AVAILABLE" "$NGINX_ENABLED"

echo "==> Testing nginx + reloading"
nginx -t
systemctl reload nginx

echo ""
echo "================================================================"
echo "  Pain Miner deployed."
echo "================================================================"
echo "URL:  https://painminer.muisbien.com"
echo "User: $PAINMINER_USER"
if [[ "$PAINMINER_PASS_GENERATED" -eq 1 ]]; then
    echo "Pass: $PAINMINER_PASS    <-- GENERATED — save this now"
else
    echo "Pass: (from .env PAINMINER_PASS)"
fi
echo ""
echo "Next steps:"
echo "  1. Verify /etc/ssl/cloudflare/origin.{crt,key} exist."
echo "  2. Confirm DNS A record painminer.muisbien.com → server IP, Proxy ON."
echo "  3. Confirm schema/pain_miner.sql ran in Supabase AND pain_miner_secrets"
echo "     has the same value as PAINMINER_RPC_SECRET in .env."
echo "  4. Set up venv + install deps:"
echo "       cd $REPO_DIR && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
echo "  5. Install cron entries (see deploy/crontab.example)."
echo "  6. Seed the queue:"
echo "       cd $REPO_DIR && venv/bin/python3 scripts/pain_miner.py --mode discover"
echo ""
echo "To add users:     sudo htpasswd $HTPASSWD_FILE <username>"
echo "To rotate secret: update PAINMINER_RPC_SECRET in .env, update Supabase, re-run deploy."
echo ""
