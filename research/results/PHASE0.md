# Phase 0 results — 24 September 2026

Data: NSE bhavcopies (cash + F&O) and PR corporate-action lists, 2017-01-02 to 2026-09-18 (2,408 sessions),
downloaded with `arth.data.nse_archive`. Panel: `arth.data.panel` (3,393 entities, 797 stopped trading,
687 split/bonus adjustments). Index history: Upstox public candles (`arth.data.indices`).

| Gate | Result | Verdict |
|---|---|---|
| 1 Data integrity | 0.37% of stock-days differ from Upstox by >0.5%; demergers unadjusted (Arth held one: STAR, Dec 2024) | In progress |
| 2 Survivorship repair | Arth core 23.1% (Upstox data 23.5%); 50% haircut on every delisting 23.3% | Passed |
| 3 Index calibration | Replica 17.5% vs Nifty200 Momentum 30 12.8%, corr 0.92 | Failed |
| 4 Go / no-go | After tax 2018-2026: Arth 19.6% vs fund 15.8% (+3.8) | Passed |
| 5 Out-of-sample | Fresh start Jan 2022, after tax: 11.3% vs 13.7% (-2.3) | Failed |
| 6 Bootstrap | P(Arth trails fund over 3 years, pre-tax) = 28.5% | Done |

Fix made during phase 0: the Rs 30 price floor now uses the traded (unadjusted) price (was a look-ahead; -0.8 pt).

Real option prices: IV of strikes sold / India VIX = put 1.11, call 0.82, ATM 0.90. Strangle (traded strikes only,
stop at 2x credit, 50% margin use, VIX-scaled margin): 9.4% incl. 5% interest on cash; 4.3% excess over cash.
Momentum + strangle stack changes Arth's after-tax return by -0.2 to -0.7 pt -> dropped.

Reproduce: see README (phase 0 commands). Full numbers: gates.json, strangle_real.json.
