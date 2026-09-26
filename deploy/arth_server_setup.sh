#!/usr/bin/env bash
# Runs ON THE SERVER (copied there by arth_deploy.ps1). Installs or upgrades the Arth paper desk from the
# newest ~/arth_repo_*.zip, runs one evening cycle, and checks the timer and dashboard. Safe to re-run:
# an existing ledger in /opt/arth/app/data is kept.
set -euo pipefail
exec > >(tee -a "$HOME/arth_install.log") 2>&1
echo "== Arth server setup, $(date -Is)"

. /etc/os-release
echo "OS: $PRETTY_NAME"
case "${ID:-}" in ubuntu|debian) ;; *) echo "STOP: this installer is for Ubuntu or Debian; found ${ID:-unknown}"; exit 1 ;; esac
mem=$(free -m | awk '/Mem:/{print $2}'); disk=$(df -Pm "$HOME" | awk 'NR==2{print $4}')
echo "CPUs: $(nproc) | RAM: ${mem} MB | free disk: ${disk} MB"
[ "$disk" -lt 3000 ] && { echo "STOP: need about 3 GB of free disk (history files + Python packages)"; exit 1; }
[ "$mem" -lt 1500 ] && echo "WARNING: under 1.5 GB of RAM; the panel build may run out of memory (add swap if it fails)"

if [ "$(id -u)" -eq 0 ]; then
  command -v sudo >/dev/null || { apt-get update -y && apt-get install -y sudo; }
else
  echo "Checking sudo (type your server password if asked):"
  sudo -v
  # keep sudo alive for the whole install (about 15 minutes) so it does not ask again halfway
  ( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &
fi

py=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0)
echo "Python: $py"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "STOP: Python 3.10 or newer is needed (Ubuntu 22.04 or 24.04). This server has $py."; exit 1; }

command -v unzip >/dev/null || { sudo apt-get update -y && sudo apt-get install -y unzip; }
ZIP=$(ls -t "$HOME"/arth_repo_*.zip | head -1)
echo "Code: $ZIP"
rm -rf "$HOME/arth" && unzip -q "$ZIP" -d "$HOME"
cd "$HOME/arth"
[ -f /opt/arth/app/data/paper.sqlite ] && echo "Existing ledger found in /opt/arth/app/data: it is kept; only the code is upgraded."

echo "== Installing (history download takes 5 to 20 minutes; NSE rate-limits)"
bash deploy/install.sh

echo "== One evening cycle: update data, process sessions, write the dashboard"
set +e
sudo -u arth bash -c "cd /opt/arth/app && /opt/arth/venv/bin/python -m arth.paper daily"
rc=$?
set -e
[ $rc -eq 2 ] && echo "(Today's NSE file is not out yet; that is normal before about 18:30 IST. The timer retries tonight.)"
[ $rc -ne 0 ] && [ $rc -ne 2 ] && { echo "STOP: the evening cycle failed (exit $rc); see the lines above"; exit $rc; }

echo "== Checks"
sudo -u arth bash -c "cd /opt/arth/app && /opt/arth/venv/bin/python -m arth.paper status"
echo "timer: $(systemctl is-enabled arth-daily.timer) / dashboard service: $(systemctl is-active arth-dashboard.service)"
systemctl list-timers arth-daily.timer --no-pager | head -3
sleep 2
code=$(curl -s -o /tmp/arth_dash.html -w '%{http_code}' http://127.0.0.1:8080/ 2>/dev/null) || true
[ -n "$code" ] || code=000
if [ "$code" = "200" ] && grep -q "Arth" /tmp/arth_dash.html; then echo "dashboard: OK (HTTP 200)"; else echo "dashboard: HTTP $code"; fi
python3 - <<'EOF'
import sqlite3
con = sqlite3.connect("/opt/arth/app/data/paper.sqlite")
meta = dict(con.execute("select key, value from meta"))
print("ledger: start", meta.get("start"), "| capital", meta.get("capital"), "| code", meta.get("code_at_init"),
      "| sessions", con.execute("select count(*) from days").fetchone()[0],
      "| target list rows", con.execute("select count(*) from plans").fetchone()[0])
EOF
echo
echo "ARTH_INSTALL_OK"
echo "Dashboard from your PC: ssh -L 8080:127.0.0.1:8080 $(whoami)@<server>   then open http://localhost:8080"
