# Arth

A monthly momentum desk for NSE equities on Upstox. Built to the plan in the project doc
"Arth: build plan for a momentum desk on Upstox". DalalDesk is not a dependency.

## Layout

```
arth/
  data/       nse_archive (NSE bhavcopy + corporate actions + F&O downloader), corp_actions, panel
  strategy/   signals: universe, ensemble score, buffer selection, trend overlay (pure functions)
  portfolio/  diff: targets -> whole-share orders, sells first, 25% no-trade band
  risk/       pretrade checks, rate gate, relative loss breaker, drawdown ladder
  broker/     upstox (delivery + LIMIT only, tag de-duplication, token request)
  exec/       order state machine, idempotent tags, timeout resolution
  ledger/     fees (Upstox delivery schedule), lots (FIFO tax lots, FY tax with set-off)
  ops/        report (dashboard), notify (ntfy / Telegram), token_webhook (Upstox daily login)
  paper.py    paper broker + SQLite ledger + nightly runner
  research/   engine_research (the original research engine), aftertax, realopt (real option prices)
  backtest.py production pieces wired into a daily backtest
config/arth.yaml   every parameter; the dial
scripts/           phase0_gates.py, strangle_real.py
tests/             unit tests + golden replay
```

## Paper trading (live from the 1 October 2026 rebalance)

Runs nightly on Claude's cloud (scheduled task "Arth paper desk nightly", 20:54 IST Mon–Sat) and publishes the
dashboard and ledger to a private artifact; see deploy/RUNBOOK.md. `arth/ops/cloud.py` is that runner.

On a fresh Ubuntu server: `bash deploy/install.sh` — see deploy/RUNBOOK.md. By hand:

```bash
python -m arth.paper bootstrap --since 2024-06-01 --start 2026-10-01 --capital 1000000
python -m arth.paper daily      # every trading evening after 18:30 IST (the systemd timer does this)
python -m arth.paper status
```

The setting in use is the `max` dial in config/arth.yaml (research/results/SETTINGS.md explains the choice).
Reports: reports/index.html (dashboard) and reports/latest.txt.

## Running it free on GitHub (paper only)

`deploy/github/nightly.yml` runs the paper desk on GitHub Actions every trading evening (21:10 IST, retry 23:10), saves
the ledger to `state/ledger.json` in the repository and publishes the dashboard to GitHub Pages. It needs no secrets.
Setup: put the workflow at `.github/workflows/nightly.yml`, set Settings > Pages > Source to "GitHub Actions", and run
it once from the Actions tab. Live orders cannot run here (Upstox requires a static IP); that needs a VPS.

## Phase 0 (research repair)

```bash
pip install pandas numpy scipy pyarrow requests pyyaml pytest
mkdir -p data && cp research/results/trading_days.csv data/
python -m arth.data.nse_archive --start 2017-01-01 --end 2026-09-18 --out data/raw --workers 3 --dates-file data/trading_days.csv
python -m arth.data.nse_archive pr --start 2017-01-01 --end 2026-09-18 --out data/raw --workers 2 --dates-file data/trading_days.csv
python scripts/phase0_gates.py        # gates 1-6 -> data/gates.json
python scripts/strangle_real.py       # strangle + stack on real option prices -> data/strangle_real.json
```

The NSE archive host rate-limits; the loader retries with backoff and is resumable (re-run to fill gaps).

Results of the 24 Sep 2026 run: research/results/PHASE0.md.

## Tests

```bash
python -m pytest -q          # unit tests + golden replay (needs the research panel)
```

The golden replay holds the production pipeline to the research engine within 0.1 point of CAGR.

## Hard rules in code

- Delivery (product `D`) and LIMIT orders only; market orders are refused.
- Every order carries a tag; a tag already in the day's order book is never re-sent.
- The risk layer can only reject, shrink or halt.
- No language model sits anywhere in the order path.
