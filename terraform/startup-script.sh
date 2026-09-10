#!/usr/bin/env bash
# Runs once on first boot (GCE startup-script metadata). Installs the
# prerequisites only -- app code, .env, and the Cloudflare Tunnel credentials
# are deployed separately (they don't belong in a startup script that ends up
# in VM metadata, which is not a secrets store).
set -euo pipefail

apt-get update
apt-get install -y ca-certificates curl gnupg git

# Docker Engine + Compose plugin (official repo, not the older docker.io package).
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# cloudflared, for the Cloudflare Tunnel this bot's webhook receiver runs behind.
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | tee /etc/apt/sources.list.d/cloudflared.list
apt-get update
apt-get install -y cloudflared

# OS Login usernames are created dynamically per user at SSH time, so there's
# no fixed account to add to the docker group at boot. Use `sudo docker ...`
# / `sudo docker compose ...` when SSHed in (OS Login admin role grants sudo).
mkdir -p /opt/social-comment-bot
