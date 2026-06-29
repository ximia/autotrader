# Polymarket Copy-Trader

Automatically copy the best Polymarket traders into your own wallet, track your
PnL and win rate, and watch it all on a live dashboard.

It:

- **finds top traders** from the Polymarket leaderboard
- **pulls their recent trades** every few minutes
- **mirrors those trades** to your wallet, sized proportionally to your bankroll
- **tracks your PnL and win rate** in a local ledger
- **runs on a loop** (default every 3 minutes)
- **shows everything in a live dashboard** at `http://localhost:8000`

> ### ⚠️ Read this first — real money
> When `LIVE_TRADING=true`, this software signs and submits **real orders** on
> Polygon using your wallet's private key. Bugs, slippage, bad fills, or copying
> a trader who blows up can lose real funds, and a leaked key can drain your
> wallet entirely. **It ships in paper mode by default** and simulates every
> trade until you explicitly opt in. You own the risk. Start in paper mode, read
> the code, and only go live with money you can afford to lose.

---

## How it works

```
 leaderboard ──► pick top N traders ──► pull each trader's new fills
                                              │
                       (per-trader timestamp cursor → no double-copies)
                                              ▼
   filter (open market? slippage? caps? already held?) ──► size (∝ bankroll)
                                              │
                  ┌───────────────────────────┴───────────────────────┐
            PaperExecutor (default)                        LiveExecutor (opt-in)
          simulates fill @ market price              FOK market order via CLOB
                                              │
                              record CopyTrade + update Position
                                              ▼
                       PnL / win-rate snapshot ──► live dashboard
```

### Sizing (proportional to bankroll)

```
our_usd = (their_trade_usd / their_portfolio_value) * my_bankroll * COPY_RATIO
          clamped to [MIN_ORDER_USD, MAX_PER_TRADE_USD]
```

We mirror the trader's *conviction* — the fraction of their portfolio a trade
represents — applied to your bankroll. In paper mode `my_bankroll` is
`PAPER_BANKROLL` plus the marked value of open positions; in live mode it's your
real on-chain portfolio value.

### Safety rails

- **Paper by default** — `LIVE_TRADING=false` simulates everything, no network writes.
- **Live pre-flight** — won't place real orders unless the wallet is funded **and**
  approved; otherwise it shows a banner telling you exactly what to fix.
- **Cash-aware sizing** — live orders are sized off (and clamped to) your actual
  available USDC, so it never tries to spend money you don't have.
- **Protective exits** — take-profit, stop-loss, and mirror-exit auto-sell to lock
  gains, cut losses, and follow a trader out.
- **Spend caps** — `MAX_PER_TRADE_USD`, `MAX_DAILY_SPEND_USD`, `MAX_OPEN_POSITIONS`.
- **Price guards** — skip if price moved more than `MAX_SLIPPAGE_PCT` from the
  trader's fill, or if the outcome is already priced above `MAX_ENTRY_PRICE`.
- **Kill switch** — Pause/Resume button (and `BotState.paused`) halts the loop instantly.
- **Idempotency** — a per-trader timestamp cursor + a unique constraint on the
  source fill id mean the same trade is never copied twice, even across restarts.
- **Secrets** — the private key is read only from `.env` (git-ignored) and is
  never logged or returned by any API route.

---

## See it working in 30 seconds (demo mode)

No wallet, no API keys, no network — synthetic top traders and markets so you can
watch the whole thing run:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh demo          # or: DEMO_MODE=true uvicorn app.web.app:app
```

Open **http://localhost:8000**. Within a few seconds the dashboard fills with
tracked traders, copied trades, open positions, and a moving PnL — all simulated
offline. This is the fastest way to confirm everything works end-to-end.

## Quick start (paper mode, real leaderboard)

```bash
cp .env.example .env          # defaults are paper-mode; no key needed
./run.sh                       # or: uvicorn app.web.app:app
```

Same dashboard, but now driven by Polymarket's **live** leaderboard and trades —
still simulating fills, so zero real orders. The loop runs on startup and every
`POLL_INTERVAL_MIN` minutes; hit **Run now** to trigger a cycle immediately.

State lives in `autotrader.db` (SQLite). Delete it to reset your run.

> Tip: set `INITIAL_LOOKBACK_MIN=10` to copy each trader's trades from the last
> 10 minutes on startup, instead of only trades that happen after the bot starts.

---

## Connecting your real Polymarket wallet (live trading)

> No copy-trader can guarantee profit — you enter slightly after the trader,
> leaderboards are survivorship-biased, and you can't see a trader's full
> strategy. This bot optimizes the *mechanics* (fast copying, slippage/price
> guards, take-profit/stop-loss, cash-aware sizing) and protects your downside.
> Trade only money you can afford to lose, and **use a fresh wallet**, not your main one.

### Which wallet do I have?

| How you use Polymarket | Wallet type | `SIGNATURE_TYPE` | `WALLET_ADDRESS` is… |
|---|---|---|---|
| Signed up with **email/phone** | Magic embedded | `1` | your Polymarket **deposit** address |
| Connected **MetaMask/Coinbase** in the browser | browser proxy | `2` | your Polymarket **deposit** address |
| You hold a **seed phrase**, fund it directly on Polygon | self-custody EOA | `0` | **your own** wallet address |

In all cases `PRIVATE_KEY` is your **signing** wallet's key. Polymarket lets you
export it from **Settings → Export Private Key** (email/browser wallets); for a
self-custody wallet, export from MetaMask (Account details → Show private key).

### Steps

1. **Fund it.** Put **USDC** (what you trade) and a little **POL** (gas) on
   **Polygon** in the wallet. Email/browser users: just deposit on Polymarket.

2. **Set approvals** (one-time). The simplest way: **place one small trade
   manually on polymarket.com** with this wallet — it sets the on-chain
   approvals the bot needs. (Email/Magic wallets often have these already.)

3. **Fill in `.env`** (`PRIVATE_KEY`, `WALLET_ADDRESS`, `SIGNATURE_TYPE` from the
   table) and keep `LIVE_TRADING=false` for now.

4. **Verify the connection — places no orders:**

   ```bash
   python -m app.tools.check_wallet
   ```

   It prints your signer + funder address, USDC balance, and approval status,
   ending in **READY TO TRADE** or exactly what to fix.

5. **Only after READY**, set `LIVE_TRADING=true` and start with tiny caps:

   ```ini
   LIVE_TRADING=true
   MAX_PER_TRADE_USD=2
   MAX_DAILY_SPEND_USD=10
   ```

   Restart. The badge turns red **● LIVE** and a banner shows your available
   USDC. If the wallet isn't funded/approved, the bot **refuses to trade** and
   tells you why instead of failing mid-order. Watch the first cycles; **Pause**
   stops everything instantly.

> Live trading can only be enabled from `.env`, never the dashboard. If your
> wallet isn't ready, the bot tracks and displays but places zero orders.

**Approval contract addresses** (if setting them by hand instead of via a manual trade):
USDC `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`, Conditional Tokens
`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`; spenders
`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`,
`0xC5d563A36AE78145C45a50134d48A1215220f80a`,
`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`.

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated full list):

| Key | Default | Meaning |
|-----|---------|---------|
| `DEMO_MODE` | `false` | Offline synthetic data; always paper |
| `LIVE_TRADING` | `false` | Real orders vs. simulation |
| `POLL_INTERVAL_MIN` | `1` | Loop frequency (minutes) |
| `LEADERBOARD_REFRESH_MIN` | `15` | How often to re-rank traders |
| `INITIAL_LOOKBACK_MIN` | `0` | Copy each trader's last N min on first sight |
| `TOP_N` | `10` | How many leaderboard traders to track |
| `LEADERBOARD_WINDOW` | `MONTH` | `DAY`/`WEEK`/`MONTH`/`ALL` |
| `TRADER_ALLOWLIST` / `TRADER_BLOCKLIST` | — | Pin or exclude wallets |
| `COPY_RATIO` | `1.0` | Scales every copied size |
| `MIN_ORDER_USD` / `MAX_PER_TRADE_USD` | `1` / `5` | Per-trade clamp |
| `MAX_DAILY_SPEND_USD` | `50` | Daily spend cap |
| `MAX_OPEN_POSITIONS` | `25` | Concurrent position cap |
| `MAX_SLIPPAGE_PCT` | `0.03` | Skip if price moved more than this |
| `MAX_ENTRY_PRICE` | `0.97` | Don't buy outcomes priced above this |
| `MIN_TRADE_USD` | `10` | Ignore source trades smaller than this |
| `PAPER_BANKROLL` | `1000` | Starting bankroll in paper mode |
| `ENABLE_AUTO_EXITS` | `true` | Take-profit / stop-loss auto-selling |
| `TAKE_PROFIT_PCT` | `0.5` | Sell when a position is up this much |
| `STOP_LOSS_PCT` | `0.3` | Sell when a position is down this much |
| `MIRROR_EXITS` | `true` | Sell when the copied trader sells |

---

## Project layout

```
app/
  config.py              # typed settings (.env); never logs the key
  db.py / models.py      # SQLAlchemy + SQLite state
  polymarket/
    data_client.py       # Data API: leaderboard / trades / positions / value
    gamma_client.py      # Gamma API: market + token metadata, open/closed status
    clob_client.py       # py-clob-client wrapper (live order placement)
  copier/
    sizing.py            # proportional-to-bankroll sizing
    executor.py          # PaperExecutor (default) + LiveExecutor
    engine.py            # the copy loop (run_once)
  pnl.py                 # realized/unrealized PnL + win rate + equity snapshots
  scheduler.py           # APScheduler loop
  web/
    app.py               # FastAPI: dashboard + JSON API + controls
    templates/ static/   # the live dashboard
tests/                   # pytest: sizing, parsing, paper executor, engine end-to-end
```

## Tests

```bash
pytest -q          # 19 tests, no network required
```

Covers sizing/clamps, Data API parsing, the paper executor's fill math, and the
full engine path (copy, idempotency, slippage/closed-market/cap skips, kill switch).

## APIs used

- **Data API** `https://data-api.polymarket.com` — `/leaderboard`, `/trades`,
  `/positions`, `/value` (public, no auth)
- **Gamma API** `https://gamma-api.polymarket.com` — market/token metadata
- **CLOB** `https://clob.polymarket.com` via `py-clob-client` — order signing/placement

> **Note on restricted networks:** some sandboxed environments block outbound
> access to `data-api.polymarket.com`. If the leaderboard/trades come back empty
> with proxy/403 errors, run the app somewhere with unrestricted outbound HTTPS.

## Scope / not included

Multi-user accounts and auth, hosted deployment, exit strategies beyond
mirroring, and non-binary/scalar markets are out of scope for this version.
```
