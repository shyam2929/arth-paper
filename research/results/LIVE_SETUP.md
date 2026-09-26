# Arth paper desk: live setup (25 Sep 2026)

## Where it runs
- **Runner:** Claude scheduled task "Arth paper desk nightly" (id trig_01WkGgujn84Ft5JtH61z8x2G).
  - Mon–Sat at 20:54 IST (cron `24 15 * * 1-6` UTC).
  - Push and email notification on each run.
- **State and dashboard:** private artifact "Arth paper desk" (https://claude.ai/artifact/PZyGroLF2YhHTP6Ri4gEop). It holds:
  - `index.html`: the dashboard;
  - `code/arth_bundle.json`: the code at tag paper-v1.1;
  - `state/ledger.json`: the ledger.
- **Why not the VPS:** this session had no SSH access to the user's server, and the linked Windows PC had no shell bridge. Paper trading needs no broker login or static IP, so the cloud route has no functional loss. Live orders later still need the server (the static-IP rule has applied since 1 Apr 2026).

## What a run does
1. Reads the artifact.
2. Unpacks the code.
3. `python -m arth.ops.cloud fetch`: fetches 500 calendar days (about 340 sessions) of NSE bhavcopy and corporate-action files, plus the Upstox index candles. A cold start takes about 2.5 minutes.
4. `python -m arth.ops.cloud nightly`:
   - restores the ledger;
   - rebuilds the panel;
   - processes the new sessions;
   - writes the page and the ledger.
5. Republishes the page and ledger.

- **Proof the 500-day window is enough:** it gives identical targets to the full history on the 24 Sep and 30 Jun 2026 signal dates.
- **Test fire, 25 Sep 01:09 IST:** succeeded. Data through 24 Sep. The indicative list matched the full-history panel exactly.

## Guards
- A failed run publishes nothing.
- A run never publishes a ledger with fewer days or fills than the one it read.
- A run never overwrites a newer artifact version.
- A gap in NSE price files is fatal.
- If a session's corporate-action list isn't out yet, that session is processed on the next run.
- Symbol changes move the holding to the renamed entity.
- A split or bonus listed only after its ex-date is applied late, exactly once (tests cover all of these; 31 pass).
- If the page hasn't been updated for more than 50 hours, the status pill turns amber.

## Code changes since the 24 Sep zip
- Changed or added (all ops): `arth/paper.py`, `arth/ops/report.py`, `arth/ops/cloud.py`, tests.
- Strategy, portfolio, risk, ledger, data and config files are byte-identical to the zip (verified with cmp).
- `.gitignore` had hidden `arth/data/` from git; now tracked.

## Timeline
- **Wed 30 Sep, 20:54 IST:** the final target list is ranked on the 30 Sep close.
- **Thu 1 Oct:** first paper rebalance at the official close (15 buys, ₹10 lakh). It shows on the dashboard after the 20:54 run, or on the Fri 2 Oct run if NSE files are late.
- **Next rebalances:** Mon 2 Nov, Tue 1 Dec 2026.

## Known risks (things that can delay or break a run)
- **NSE files published after 20:54:** a one-session lag with identical fills.
- **NSE 403 blocks from the cloud IP:** the run fails with no publish, and the next night retries.
- **Upstox public candle endpoint outage:** the calendar or index fetch fails, so the run fails.
- **Approval prompts in scheduled runs:** the task was created without automatic approval. The user should switch on "Automatically approve" in the task settings.
- **2027 NSE holidays:** not in `arth/data/calendar.py`. Add them when NSE publishes the list (usually in December).

## 26 Sep 2026: server install kit (paper-v1.2)
- **Bugs found and fixed before they reached the server:**
  - **`install.sh` left out the code's data package.** Its rsync rule `--exclude data` also matched `arth/data/`, so the installed app had no data package and would have crashed on the first nightly run. The rule is now `--exclude /data`.
  - **The panel builder crashed on pandas 2.x.** It mixed numpy datetimes with pandas Timestamps; any Ubuntu 22.04 / Python 3.10 server would have hit this. Fixed.
  - **Results are unchanged.** The panel is identical under pandas 3.0. Fills and equity under pandas 2.2 match to the paisa.
- **Full install tested end to end** on Ubuntu 24.04, with systemd stubbed because the test container can't run it:
  - Took 4m43s. Downloaded history since June 2024, ran 21 tests, created the ledger for 1 Oct with ₹10 lakh, and ran one evening cycle.
  - The dashboard served HTTP 200.
  - The target list matched the cloud runner's exactly: same 15 names, same order.
  - Re-running upgrades the code and keeps the existing ledger.
- **Kit on the user's PC:** `C:\Users\shyam\Downloads\arth_deploy`.
  - To install, the user double-clicks `arth_deploy.cmd`. It asks for the server login (user@address) and port, then copies the code across and runs `arth_server_setup.sh` over SSH.
  - The user types their own passwords.
  - Computer use can't type into terminals, so Claude only opens the folder and watches the terminal.
- **Status: paused.** The user didn't have the server details to hand on 26 Sep.
- **Cloud runner:** the bundle was updated to paper-v1.2. The 25 Sep evening run published late, so the task's "Automatically approve" setting is still needed.
