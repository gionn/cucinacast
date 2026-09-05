#!/usr/bin/env bash
# Set up CucinaCast as a systemd service after cloning the repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"
SERVICE_NAME=cucinacast

case "$REPO_DIR" in
    *[[:space:]]*)
        echo "Error: repo path '${REPO_DIR}' contains whitespace, which the systemd unit can't handle. Move the repo to a path without spaces." >&2
        exit 1
        ;;
esac

cd "$REPO_DIR"

if [ ! -d .venv ]; then
    if [ -n "${SUDO_USER:-}" ]; then
        sudo -u "${SUDO_USER}" python3 -m venv .venv
    else
        python3 -m venv .venv
    fi
fi
if [ -n "${SUDO_USER:-}" ]; then
    sudo -u "${SUDO_USER}" ./.venv/bin/pip install -q -r requirements.txt
else
    ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
    cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=
NEST_DEVICE_NAME=
OWNER_USER_ID=
ALLOWED_USER_IDS=
EOF
    chmod 600 .env
    [ -n "${SUDO_USER:-}" ] && chown "${SUDO_USER}" .env
    echo "Created .env — fill in TELEGRAM_BOT_TOKEN, NEST_DEVICE_NAME and OWNER_USER_ID before starting the service."
fi

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=CucinaCast Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python ${REPO_DIR}/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"

echo
echo "Service installed as ${SERVICE_NAME}.service, running as user '${SERVICE_USER}'."
echo "Edit ${REPO_DIR}/.env, then start it with:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo "Check logs with:"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
