#!/usr/bin/env bash
# Arth paper desk: one-command install on a fresh Ubuntu 22.04/24.04 server (run as a sudo-capable user).
#   bash deploy/install.sh            # from inside the unzipped repo
set -euo pipefail
APP=/opt/arth/app
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git rsync tzdata
id arth >/dev/null 2>&1 || sudo useradd -r -m -d /opt/arth -s /bin/bash arth
sudo mkdir -p "$APP"
sudo rsync -a --delete --exclude /data --exclude /reports --exclude .git ./ "$APP/"
sudo mkdir -p "$APP/data" "$APP/reports"
sudo chown -R arth:arth /opt/arth
sudo -u arth python3 -m venv /opt/arth/venv
sudo -u arth /opt/arth/venv/bin/pip install -q -r "$APP/requirements.txt"
[ -f /etc/arth.env ] || sudo install -m 600 -o arth -g arth deploy/arth.env.example /etc/arth.env
sudo install -m 644 deploy/arth-daily.service deploy/arth-daily.timer deploy/arth-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
# history since mid-2024 (10-20 minutes), then a paper ledger starting 1 Oct 2026 with Rs 10 lakh
sudo -u arth bash -c "cd $APP && /opt/arth/venv/bin/python -m arth.paper bootstrap --since 2024-06-01 --start 2026-10-01 --capital 1000000"
sudo -u arth bash -c "cd $APP && /opt/arth/venv/bin/python -m pytest -q tests/test_units.py tests/test_paper.py -k 'not replay'"
sudo systemctl enable --now arth-daily.timer arth-dashboard.service
systemctl list-timers arth-daily.timer --no-pager
echo "Installed. Dashboard: ssh -L 8080:127.0.0.1:8080 <server>, then open http://localhost:8080"
