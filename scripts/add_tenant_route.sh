#!/usr/bin/env bash
# Run on the VM (needs sudo) after `python -m billing.cli provision --start`.
# Adds a Cloudflare Tunnel ingress rule routing <tenant_key>.<domain> to the
# tenant's port, then reloads cloudflared. See deploy/cloudflared/config.yml.example
# for the expected shape of the live config this edits.
set -euo pipefail

TENANT_KEY="${1:?Usage: add_tenant_route.sh <tenant_key> <port> [domain]}"
PORT="${2:?Usage: add_tenant_route.sh <tenant_key> <port> [domain]}"
DOMAIN="${3:-hindolroad.download}"
CONFIG="${CLOUDFLARED_CONFIG:-/etc/cloudflared/config.yml}"
TUNNEL_NAME="${CLOUDFLARE_TUNNEL_NAME:?Set CLOUDFLARE_TUNNEL_NAME to the cloudflared tunnel name for this VM}"

HOSTNAME="${TENANT_KEY}.${DOMAIN}"

if [[ ! -f "$CONFIG" ]]; then
  echo "cloudflared config not found at $CONFIG (see deploy/cloudflared/config.yml.example)" >&2
  exit 1
fi

if grep -q "hostname: ${HOSTNAME}$" "$CONFIG"; then
  echo "Ingress rule for ${HOSTNAME} already present in ${CONFIG}; skipping insert."
else
  cp "$CONFIG" "${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$CONFIG" "$HOSTNAME" "$PORT" <<'PY'
import sys

config_path, hostname, port = sys.argv[1:4]
with open(config_path) as f:
    lines = f.readlines()
catch_all_idx = next(
    i for i, line in enumerate(lines) if "http_status:404" in line
)
insert = [
    f"  - hostname: {hostname}\n",
    f"    service: http://localhost:{port}\n",
]
lines[catch_all_idx:catch_all_idx] = insert
with open(config_path, "w") as f:
    f.writelines(lines)
PY
  echo "Inserted ingress rule for ${HOSTNAME} -> localhost:${PORT} into ${CONFIG}"
fi

cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"
systemctl reload cloudflared 2>/dev/null || systemctl restart cloudflared

echo "Done. https://${HOSTNAME}/ now routes to localhost:${PORT}."
