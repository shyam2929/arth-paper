# Strategy settings compared — 24 September 2026

Survivorship-free NSE data, Feb 2018 – 18 Sep 2026, Rs 10 lakh, all Upstox charges, 0.15% slippage, before tax.
Nifty Midcap150 Momentum 50 (price) for reference: 17.1% (2018-21: 19.4%, 2022-26: 15.0%).

Selection rule, fixed before choosing: highest CAGR in the WORSE of the two halves (2018-21, 2022-26),
so a setting cannot win on one lucky regime.

| Setting | CAGR | Max DD | Months >= +6% | Worst month | 2018-21 | 2022-26 | Fresh start 2022 |
|---|---|---|---|---|---|---|---|
| 12-1, 15 stocks, universe 1000 | 37.5% | -40.4% | 36.9% | -21.1% | 43.4% | 32.5% | 33.2% |
| 12-1, 20 stocks, universe 1000 | 32.7% | -40.8% | 35.9% | -24.5% | 35.4% | 30.1% | 30.1% |
| 15 stocks, score = 12-1 only, overlay off | 30.4% | -41.7% | 32.0% | -21.4% | 31.3% | 29.3% | 29.7% |
| 15 stocks, universe 1000 (min Rs 2 Cr/day), overlay off | 35.3% | -42.8% | 36.9% | -24.9% | 45.6% | 27.0% | 26.8% |
| 12-1, 20 stocks, universe 500 | 27.4% | -44.8% | 32.0% | -24.2% | 28.8% | 25.8% | 25.6% |
| 15 stocks, inverse-vol weights, overlay off | 29.2% | -43.2% | 32.0% | -21.4% | 34.0% | 24.9% | 22.1% |
| 15 stocks, weekly rebalance, overlay off | 25.7% | -49.4% | 34.0% | -21.6% | 27.3% | 24.0% | 23.7% |
| 10 stocks, universe 1000, overlay off | 37.9% | -45.4% | 35.0% | -22.7% | 57.5% | 23.2% | 22.2% |
| 12-1, 10 stocks, universe 500 | 32.0% | -48.2% | 33.0% | -22.3% | 43.4% | 23.1% | 24.0% |
| Aggressive dial: 15 stocks, overlay off | 28.5% | -48.5% | 31.1% | -23.6% | 35.5% | 22.5% | 20.1% |
| 30 stocks, overlay off | 25.2% | -40.3% | 30.1% | -24.5% | 28.2% | 22.4% | 22.9% |
| 20 stocks, overlay off | 25.3% | -43.9% | 32.0% | -25.4% | 29.5% | 21.5% | 20.8% |
| 12-1, 15 stocks, universe 500, overlay on | 24.0% | -43.6% | 27.2% | -20.2% | 26.6% | 21.5% | 21.8% |
| 15 stocks, universe 250, overlay off | 23.4% | -45.8% | 32.0% | -24.0% | 25.8% | 21.2% | 21.4% |
| 12-1, 10 stocks, universe 1000 | 33.8% | -48.0% | 36.9% | -23.7% | 51.8% | 20.2% | 20.1% |
| 15 stocks, score = NMS only, overlay off | 23.2% | -47.4% | 29.1% | -21.9% | 27.9% | 19.0% | 17.9% |
| Defensive dial: 25 stocks, overlay on | 22.8% | -30.1% | 25.2% | -17.9% | 27.8% | 18.3% | 18.0% |
| 10 stocks, overlay off | 28.3% | -49.5% | 31.1% | -22.5% | 40.9% | 18.3% | 19.2% |
| Core dial: 15 stocks, overlay on | 23.1% | -34.7% | 27.2% | -18.8% | 31.6% | 16.1% | 13.6% |
| 15 stocks, all cash when risk-off | 16.7% | -36.2% | 18.4% | -19.0% | 26.2% | 9.0% | 7.9% |

Chosen: **max dial** = 12-1 score, 15 stocks, most liquid 1,000 names with >= Rs 2 crore median daily value, no trend overlay, monthly.
After tax (FIFO lots, today's rates): 31.2% vs fund 15.8% (2018-2026); from a fresh Jan 2022 start 27.0% vs 13.7%.
Mean month +3.07%, median +3.46%, 36.9% of months >= +6%, 35% negative, worst -21.1%.

Caveat: this is the best of ~55 variants tried across the project. Its neighbours (12-1 with 20 stocks: 32.7%;
12-1 on the liquid 500: 30.4%) are also strong, which argues for a real effect, but expect live results well below
the backtest. Deflated Sharpe (crude, 55 trials) ~0.91. No further search: this setting is frozen for the paper run.
