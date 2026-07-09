#!/bin/bash
# ──────────────────────────────────────────────────────────────
# install.sh — Ubuntu deployment script for BTC Spot Scalper
# Run as:  sudo bash deploy/install.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="/opt/btc-scalper"
APP_USER="www-data"
SERVICE_NAME="btc-scalper"

echo "══════════════════════════════════════════════════"
echo "  BTC Spot Scalper — Ubuntu Installation Script"
echo "══════════════════════════════════════════════════"

# ── Check root ──
if [[ $EUID -ne 0 ]]; then
    echo "❌ This script must be run as root (use sudo)."
    exit 1
fi

# ── Install system dependencies ──
echo "▶ Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    nginx \
    build-essential \
    libssl-dev libffi-dev \
    curl

# ── Create application directory ──
echo "▶ Creating application directory: $APP_DIR"
mkdir -p "$APP_DIR"

# ── Copy project files (if running from repo) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [[ -f "$PROJECT_DIR/main.py" ]]; then
    echo "▶ Copying project files from $PROJECT_DIR..."
    cp -r "$PROJECT_DIR"/* "$APP_DIR/"
else
    echo "⚠️  Project files not found relative to script."
    echo "   Please manually copy the project to $APP_DIR"
fi

# ── Create .env file from template ──
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "▶ Creating .env file..."
    cat > "$APP_DIR/.env" << 'EOF'
# ─── BTC Scalper Environment ───
APP_ENV=demo
DSN=sqlite:////opt/btc-scalper/btc_scalper.db
USE_BNB_FEE=true

# ─── Binance API (leave blank — set via dashboard) ───
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET_KEY=
BINANCE_TESTNET_SECRET=

# ─── Encryption key for stored credentials ───
# Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"
ENCRYPTION_KEY=
EOF
    echo "✅ .env created at $APP_DIR/.env"
    echo "⚠️  IMPORTANT: Edit $APP_DIR/.env and set ENCRYPTION_KEY!"
fi

# ── Create virtual environment ──
echo "▶ Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip wheel setuptools -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "✅ Python dependencies installed"

# ── Generate encryption key if not set ──
if grep -q "^ENCRYPTION_KEY=$" "$APP_DIR/.env"; then
    ENC_KEY=$("$APP_DIR/venv/bin/python3" -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/^ENCRYPTION_KEY=$/ENCRYPTION_KEY=$ENC_KEY/" "$APP_DIR/.env"
    echo "✅ Generated ENCRYPTION_KEY automatically"
fi

# ── Set permissions ──
echo "▶ Setting file permissions..."
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR"

# ── Install systemd service ──
echo "▶ Installing systemd service..."
cp "$APP_DIR/deploy/btc-scalper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "✅ Service installed and enabled"

# ── Install Nginx config ──
echo "▶ Installing Nginx configuration..."
cp "$APP_DIR/deploy/nginx-btc-scalper" /etc/nginx/sites-available/btc-scalper
ln -sf /etc/nginx/sites-available/btc-scalper /etc/nginx/sites-enabled/btc-scalper

# Remove default nginx site
rm -f /etc/nginx/sites-enabled/default

# Test nginx config
if nginx -t 2>/dev/null; then
    systemctl reload nginx
    echo "✅ Nginx configured and reloaded"
else
    echo "⚠️  Nginx config test failed — check /etc/nginx/sites-available/btc-scalper"
    echo "   Edit the server_name directive to match your domain or IP."
fi

# ── Start the service ──
echo "▶ Starting $SERVICE_NAME service..."
systemctl start "$SERVICE_NAME"

# ── Verify ──
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "✅ Service is running!"
else
    echo "❌ Service failed to start — check logs:"
    echo "   journalctl -u $SERVICE_NAME -e"
fi

# ── Summary ──
echo ""
echo "══════════════════════════════════════════════════"
echo "  Installation Complete!"
echo "══════════════════════════════════════════════════"
echo ""
echo "  Dashboard:  http://localhost  (or your server IP)"
echo "  Config:     $APP_DIR/.env"
echo "  Database:   $APP_DIR/btc_scalper.db"
echo ""
echo "  Commands:"
echo "    sudo systemctl start $SERVICE_NAME"
echo "    sudo systemctl stop $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    journalctl -u $SERVICE_NAME -f   (live logs)"
echo ""
echo "  ⚠️  IMPORTANT:"
echo "    1. Set your Binance API keys via the dashboard UI"
echo "    2. Start in DEMO mode first (Testnet)"
echo "    3. Edit nginx server_name to your domain/IP"
echo "    4. Set up HTTPS with Certbot for production"
echo ""