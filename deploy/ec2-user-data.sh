#!/bin/bash
# EC2 user-data for an arena runner on Amazon Linux 2023 (arm64 / t4g).
#
# Launch it with IMDS locked down and NO IAM role -- the sandbox host should
# hold no credentials worth stealing:
#
#   aws ec2 run-instances \
#     --image-id <al2023-arm64-ami> --instance-type t4g.small \
#     --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
#     --user-data file://deploy/ec2-user-data.sh
#
set -euxo pipefail

REPO_URL=${REPO_URL:-https://github.com/CHANGEME/poker-arena.git}
ARENA_HOME=/opt/poker-arena

dnf -y update
dnf -y install docker git python3.11 python3.11-pip
systemctl enable --now docker

useradd --system --create-home --home-dir /home/arena arena
usermod -aG docker arena

git clone "$REPO_URL" "$ARENA_HOME"
chown -R arena:arena "$ARENA_HOME"

sudo -u arena python3.11 -m venv "$ARENA_HOME/.venv"
sudo -u arena "$ARENA_HOME/.venv/bin/pip" install -e "$ARENA_HOME[dev]"
sudo -u arena install -d "$ARENA_HOME/submissions" "$ARENA_HOME/results"

docker build -t poker-arena-sandbox:latest "$ARENA_HOME"
sudo -u arena "$ARENA_HOME/.venv/bin/python" -m pytest "$ARENA_HOME/tests" -q

cat > /etc/systemd/system/poker-arena.service <<UNIT
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
ExecStart=$ARENA_HOME/.venv/bin/python deploy/tournament.py submissions results
UNIT

cat > /etc/systemd/system/poker-arena.timer <<'UNIT'
[Unit]
Description=nightly poker-arena tournament

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now poker-arena.timer
