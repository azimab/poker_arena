#!/bin/bash
# EC2 user-data for an arena host on Amazon Linux 2025 (arm64 / t4g).
#
# Runs both the API (uvicorn behind nginx) and the nightly tournament timer.
# Launch it with IMDS locked down and NO IAM role. The only credentials on the
# host are the ARENA_* values below, so give the database role access to the
# arena tables and nothing else. The secrets live in the gitignored deploy/.env
# and are spliced in after the shebang at launch:
#
#   { head -n1 deploy/ec2-user-data.sh; cat deploy/.env; tail -n +2 deploy/ec2-user-data.sh; } > user-data.sh
#   aws ec2 run-instances \
#     --image-id <al2023-arm64-ami> --instance-type t4g.small \
#     --key-name <key-pair> --security-group-ids <sg-with-22-80-443> \
#     --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
#     --user-data file://user-data.sh
#
# Set ARENA_DOMAIN to a hostname already pointing at this instance to get a
# Let's Encrypt certificate; leave it empty to serve plain http for testing.
set -euo pipefail
ARENA_DATABASE_URL=${ARENA_DATABASE_URL:-postgresql://CHANGEME}
ARENA_GITHUB_CLIENT_ID=${ARENA_GITHUB_CLIENT_ID:-CHANGEME}
ARENA_GITHUB_CLIENT_SECRET=${ARENA_GITHUB_CLIENT_SECRET:-CHANGEME}
ARENA_SECRET_KEY=${ARENA_SECRET_KEY:-$(openssl rand -hex 32)}
ARENA_DOMAIN=${ARENA_DOMAIN:-arena.pokerbots.app}
ARENA_ACME_EMAIL=${ARENA_ACME_EMAIL:-abdulmajeethazim9@gmail.com}
set -x

REPO_URL=${REPO_URL:-https://github.com/azimab/poker_arena.git}
ARENA_HOME=/opt/poker-arena

dnf -y update
dnf -y install docker git nginx python3.11 python3.11-pip
systemctl enable --now docker

useradd --system --create-home --home-dir /home/arena arena
usermod -aG docker arena

git clone "$REPO_URL" "$ARENA_HOME"
chown -R arena:arena "$ARENA_HOME"

sudo -u arena python3.11 -m venv "$ARENA_HOME/.venv"
sudo -u arena "$ARENA_HOME/.venv/bin/pip" install -e "$ARENA_HOME[server,dev]"

# Without a domain the callback has to come back over plain http by IP, which
# GitHub allows but nothing else should rely on.
if [ -n "$ARENA_DOMAIN" ]; then
    ARENA_ORIGIN="https://$ARENA_DOMAIN"
else
    ARENA_ORIGIN="http://$(curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/public-ipv4)"
fi

install -m 600 -o arena -g arena /dev/null /etc/poker-arena.env
set +x
cat >/etc/poker-arena.env <<ENV
ARENA_DATABASE_URL=${ARENA_DATABASE_URL:-postgresql://CHANGEME}
ARENA_GITHUB_CLIENT_ID=${ARENA_GITHUB_CLIENT_ID:-CHANGEME}
ARENA_GITHUB_CLIENT_SECRET=${ARENA_GITHUB_CLIENT_SECRET:-CHANGEME}
ARENA_SECRET_KEY=$ARENA_SECRET_KEY
ARENA_LOGIN_REDIRECT=$ARENA_ORIGIN/
ENV
set -x

docker build -t poker-arena-sandbox:latest "$ARENA_HOME"
sudo -u arena "$ARENA_HOME/.venv/bin/python" -m pytest "$ARENA_HOME/tests" -q

cat >/etc/systemd/system/poker-arena-api.service <<UNIT
[Unit]
Description=poker-arena api
After=docker.service network-online.target
Requires=docker.service

[Service]
User=arena
WorkingDirectory=$ARENA_HOME
EnvironmentFile=/etc/poker-arena.env
# Live games, the submission-check pool and the sandbox slot semaphore are all
# in-process state, so a second worker would fork them and break both.
ExecStart=$ARENA_HOME/.venv/bin/uvicorn poker_arena.api:app \\
    --host 127.0.0.1 --port 8000 --workers 1 \\
    --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/poker-arena.service <<UNIT
[Unit]
Description=poker-arena tournament
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=arena
WorkingDirectory=$ARENA_HOME
Environment=ARENA_HANDS=100
Environment=ARENA_PACE=1.0
EnvironmentFile=/etc/poker-arena.env
ExecStart=$ARENA_HOME/.venv/bin/python deploy/tournament.py
UNIT

cat >/etc/systemd/system/poker-arena.timer <<'UNIT'
[Unit]
Description=nightly poker-arena tournament

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat >/etc/systemd/system/poker-arena-update.service <<UNIT
[Unit]
Description=deploy origin/main to poker-arena
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $ARENA_HOME/deploy/update.sh
UNIT

cat >/etc/systemd/system/poker-arena-update.timer <<'UNIT'
[Unit]
Description=poll origin/main for poker-arena deploys

[Timer]
OnCalendar=minutely

[Install]
WantedBy=timers.target
UNIT

cat >/etc/nginx/conf.d/poker-arena.conf <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${ARENA_DOMAIN:-_};

    # A 1 MiB submission plus multipart framing exceeds nginx's 1m default.
    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGINX

systemctl daemon-reload
systemctl enable --now poker-arena-api.service
systemctl enable --now nginx
systemctl enable --now poker-arena.timer
systemctl enable --now poker-arena-update.timer

# certbot rewrites the server block above for TLS and installs its own renewal timer.
if [ -n "$ARENA_DOMAIN" ]; then
    dnf -y install certbot python3-certbot-nginx
    if [ -n "$ARENA_ACME_EMAIL" ]; then
        acme_contact=(--email "$ARENA_ACME_EMAIL")
    else
        acme_contact=(--register-unsafely-without-email)
    fi
    certbot --nginx --non-interactive --agree-tos --redirect \
        -d "$ARENA_DOMAIN" "${acme_contact[@]}"
fi
