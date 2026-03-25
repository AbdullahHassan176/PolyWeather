# PolyWeather Bot

Automated Polymarket weather-market trading bot.  
Finds edge by comparing ECMWF ensemble forecasts against Polymarket prices, then bets with quarter-Kelly sizing.

## Architecture

```
main.py              — entry point + scheduler (SCAN_INTERVAL, default 5 min)
config.py            — Config dataclass, loads .env via python-dotenv
polymarket/
  markets.py         — PolymarketClient: fetches active temperature markets (Gamma API /events?tag_slug=temperature)
  parser.py          — parse_question(): regex → {city, date, threshold, direction, unit}
  prices.py          — enrich_with_live_prices() (CLOB midpoints), fetch_spread() (bid/ask per signal)
weather/
  client.py          — WeatherClient: Open-Meteo ensemble → P(outcome); falls back to regular API on hourly limit
  cities.py          — CITIES dict: city name → (lat, lon) with fuzzy matching
strategy/
  analyzer.py        — analyze() → TradeSignal with full metadata; Kelly sizing
trader/
  executor.py        — execute_trade(): FOK market order; slippage guard; returns (filled, status, actual_spent)
tracker/
  log.py             — record_signal(): appends rich 24-field JSONL record to paper_trades.jsonl
  reconcile.py       — resolves outcomes, fetches actual temps, prints multi-axis performance report
watch.py             — live dashboard: balance, open positions, P&L (py -3.11 watch.py [--live|--pnl])
```

## Data Flow

```
get_weather_markets()           Gamma API /events?tag_slug=temperature → list[market]
  ↓
enrich_with_live_prices()       CLOB /midpoints batch → replace stale Gamma prices; drop YES<5% or >95%
  ↓
parse_question(q)               regex → {city, date, threshold, direction, unit}
  ↓  filter: target_date > today_utc
WeatherClient.get_probability() Open-Meteo ensemble (50 members) OR regular API + normal(σ by horizon)
  ↓
analyze(market, parsed, prob)   edge = our_prob - market_price; Kelly sizing; min bet $5
  ↓
fetch_spread(token_id)          CLOB order book → bid, ask, spread (per-signal, logged for research)
  ↓
execute_trade(signal)           FOK market order; slippage abort if live_price > signal.price + 0.05
  ↓
record_signal(signal, status)   paper_trades.jsonl — 24 fields including forecast metadata + spread
```

## Key Design Decisions

- **Ensemble forecast**: ECMWF IFS 0.25° via Open-Meteo. Fraction of members satisfying condition + Laplace smoothing `(hits+1)/(n+2)`.  
- **Regular-API fallback**: when ensemble hourly limit hit, use point forecast + normal(σ calibrated by horizon: 1.5°C at T+1 → 4°C at T+7).  Logged as `forecast_method="regular_fallback"`.  
- **Price freshness**: Gamma prices can lag 10–70¢. CLOB midpoints fetched in one batch per scan replace them before edge calculation.  
- **Liquidity filter**: markets with YES < 5% or > 95% dropped (minority-side order books have extreme spreads).  
- **Date filter**: markets resolving today or earlier are skipped (outcome already known, no forecast value).  
- **Slippage guard**: if `create_market_order` computes a fill price more than 5¢ above signal price, trade is aborted and logged as `unfilled`.  
- **Kelly sizing**: `f* = edge / (1-price)` × `KELLY_FRACTION` × `MAX_TRADE_USDC`, min $5 (CLOB minimum).  
- **Disk cache**: forecast results saved to `weather_cache.json`; refreshed daily. Prevents re-fetching across scans.

## paper_trades.jsonl Schema (v2)

Every signal logged with: `ts`, `condition_id`, `question`, `city`, `target_date`, `direction`, `threshold`, `threshold2`, `unit`, `our_side`, `our_prob`, `market_price`, `edge`, `bet_usdc`, `forecast_horizon_days`, `forecast_method`, `forecast_mean`, `forecast_std`, `n_members`, `bid`, `ask`, `spread`, `volume_24h`, `fill_status`, `actual_spent`, `live_price`, `slippage`, `outcome`, `won`, `actual_temp`.

`fill_status`: `"dry_run"` | `"filled"` | `"unfilled"` | `None` (legacy).

## Monitoring

```bash
py -3.11 watch.py               # snapshot: balance, open positions, resolved P&L
py -3.11 watch.py --live        # auto-refresh every 30s
py -3.11 watch.py --pnl         # resolve outcomes + show P&L
py -3.11 -m tracker.reconcile   # full report: calibration, edge/city/method breakdown
Get-Content polyweather.log -Wait -Tail 50   # live log tail
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PRIVATE_KEY` | — | EOA private key |
| `PROXY_WALLET` | — | Gnosis Safe address (blank = plain EOA) |
| `POLYMARKET_API_KEY/SECRET/PASSPHRASE` | auto-derived | CLOB Level 2 credentials |
| `POLYGON_RPC_URL` | `https://1rpc.io/matic` | Polygon JSON-RPC |
| `DRY_RUN` | `true` | `false` = live trading |
| `MIN_EDGE` | `0.05` | Minimum edge to signal (5%) |
| `MAX_TRADE_USDC` | `50.0` | Kelly cap per trade |
| `KELLY_FRACTION` | `0.25` | Quarter-Kelly multiplier |
| `SCAN_INTERVAL` | `300` | Seconds between scans |

## Known Limitations / Future Work (see docs/)

- Open-Meteo ensemble API has a per-hour free-tier limit; bot falls back to regular API automatically but ensemble is more accurate.
- `volume_24h` not yet populated in paper_trades (Gamma API field name differs). Fix: map `volumeNum` in markets.py normalise.
- Winning positions must be manually redeemed on polymarket.com (or via Parsec gasless relayer with `PARSEC_API_KEY`).
- No multi-leg hedge logic (e.g. spreading across correlated markets).
