#!/usr/bin/env bash
# Deploys the Momentum Engine to the VPS (KVM2), fully isolated from the
# arbitrage bot: its own directory, its own system user, its own systemd unit.
# Usage: VPS_HOST=user@host [VPS_SSH_PORT=22] [REMOTE_DIR=/opt/robotcripto-momentum] ./deploy/deploy.sh
set -euo pipefail

: "${VPS_HOST:?set VPS_HOST=user@host}"
VPS_SSH_PORT="${VPS_SSH_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/robotcripto-momentum}"
SERVICE_NAME="robotcripto-momentum"
SSH_OPTS=(-p "$VPS_SSH_PORT")

echo "==> pre-deploy: checking arbitrage bot isn't going to be touched"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "systemctl list-units --type=service --all | grep -i arbitrage || echo 'no arbitrage-named service found (informational only)'"

echo "==> ensuring dedicated system user '$SERVICE_NAME' exists"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "id -u $SERVICE_NAME >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin $SERVICE_NAME"

echo "==> syncing code to $VPS_HOST:$REMOTE_DIR"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown \$(whoami) $REMOTE_DIR"
rsync -az -e "ssh -p $VPS_SSH_PORT" \
  --exclude ".venv" --exclude "__pycache__" --exclude "*.pyc" --exclude "db/momentum.db*" \
  --exclude ".git" \
  ./ "$VPS_HOST:$REMOTE_DIR/"

echo "==> creating/updating venv + installing deps on VPS"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "cd $REMOTE_DIR && (python3.11 -m venv .venv 2>/dev/null || python3 -m venv .venv) && ./.venv/bin/pip install -q --upgrade pip && ./.venv/bin/pip install -q -e ."

echo "==> fixing ownership for the dedicated service user"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "sudo chown -R $SERVICE_NAME:$SERVICE_NAME $REMOTE_DIR"

echo "==> installing systemd unit"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "sudo cp $REMOTE_DIR/deploy/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service && sudo systemctl daemon-reload"

echo "==> starting service"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "sudo systemctl enable --now ${SERVICE_NAME}"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "sleep 3 && sudo systemctl status ${SERVICE_NAME} --no-pager"

echo "==> post-deploy: confirming arbitrage bot's own services are still active"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "systemctl list-units --type=service --state=running | grep -i arbitrage || echo 'no arbitrage-named service found (informational only)'"

echo "==> done. tail logs with: ssh $VPS_HOST journalctl -u ${SERVICE_NAME} -f"
