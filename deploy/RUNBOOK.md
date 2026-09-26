# Arth paper desk — runbook

## Where it runs today: Claude's cloud (live since 25 Sep 2026)

No server is needed for paper trading. A Claude scheduled task, **"Arth paper desk nightly"**, runs Mon–Sat at
20:54 IST. Each run starts in a fresh cloud session and does the following:

1. Reads the artifact **Arth paper desk** (private to you). The artifact holds three things:
   - the dashboard (`index.html`);
   - the code (`code/arth_bundle.json`, tag `paper-v1.1`);
   - the ledger (`state/ledger.json`).
2. Unpacks the code and runs `python -m arth.ops.cloud fetch`. This pulls about 340 sessions of NSE bhavcopy and corporate-action files (500 calendar days) plus the index candles. A cold start takes about 2½ minutes.
3. Runs `python -m arth.ops.cloud nightly`. This restores the ledger, processes every new session, and writes the dashboard and the updated ledger.
4. Republishes the artifact. A failed run publishes nothing; the page keeps the last good state. If no run has updated the page for more than 50 hours, the status pill turns amber.

Guards:
- A run never publishes a ledger with fewer sessions or fills than the one it started from.
- A run never overwrites a newer version of the artifact.
- A gap in NSE price files is fatal.
- If a session's corporate-action list is not out yet, that session waits for the next run.

Moving to a server later (needed for live orders: Upstox requires a static IP): take the latest `state/ledger.json` from
the artifact, run `python -m arth.ops.cloud restore --ledger ledger.json` (it writes `data/paper.sqlite`), then follow
the server sections below, skipping `init`. Disable the scheduled task first so two runners never write the same ledger.

## Server route (for live trading later)

## What runs, and when

Every trading evening at 19:15 IST (retries 21:00 and 23:15, catch-up Saturday 10:00) `arth-daily` does:

1. Downloads NSE's closing-price file and corporate-action list for any trading day not yet on disk.
2. Rebuilds the adjusted price panel.
3. Processes each new session in date order:
   - **Splits and bonuses** change the share counts.
   - **The first session of each month is a rebalance.** Targets come from the previous session's close, and orders fill at that session's official close ± 0.15% with Upstox charges.
4. Writes `reports/index.html` and `reports/latest.txt`, and pushes the summary to your phone.

Rebalance sessions this year: **Thu 1 Oct 2026**, **Mon 2 Nov 2026**, **Tue 1 Dec 2026**.
The evening before each one, the dashboard shows the target list.

Every run is idempotent. If the server was down, the next run catches up with identical results, because fills use published closing prices. The ledger records the code version used for every day.

## Install (fresh Ubuntu 22.04/24.04 server)

```bash
unzip arth_repo.zip && cd arth
bash deploy/install.sh
sudo nano /etc/arth.env          # optional: ntfy topic or Telegram bot for alerts
```

`install.sh` does four things:
- Creates the `arth` user.
- Installs Python packages.
- Fetches history since June 2024 (10–20 minutes; NSE rate-limits).
- Creates the paper ledger (start 1 Oct 2026, ₹10 lakh), runs the tests, and enables the timer and dashboard.

## Look at it

```bash
ssh -L 8080:127.0.0.1:8080 you@server      # then open http://localhost:8080
sudo -u arth /opt/arth/venv/bin/python -m arth.paper status   # one-line summary (run in /opt/arth/app)
systemctl list-timers arth-daily.timer
journalctl -u arth-daily -n 100 --no-pager
```

## When something goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| `data not ready` in the journal, exit 2 | NSE's file isn't published yet | Nothing; the 21:00 or 23:15 run picks it up |
| Alert "data update FAILED" | NSE blocked the server's IP or changed a file format | Run `python -m arth.data.update` by hand. If it's blocked, tell Claude: the fallback is Upstox candles |
| No run on a trading day | Server was off | Nothing; the next run catches up |
| `rejected ...` lines in the log | A pre-trade check refused a paper order | Read the reason; checks may only shrink or halt |

## Rules while paper trading

- The setting is frozen (`config/arth.yaml`, dial `max`). Changes only in a quarterly window, with a note in `research/prereg/`.
- Don't edit the ledger by hand. To restart from scratch: `python -m arth.paper init --start YYYY-MM-DD --force`.
