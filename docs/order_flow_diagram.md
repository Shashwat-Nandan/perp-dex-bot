# Order Placement Flow — Constraint Diagram

Use this document to manually trace through each stage of the order flow and identify where the process is failing. Each gate lists what is checked, the config variable, its default, and what to verify.

---

## 1. HIGH-LEVEL FLOW

```
┌─────────────────────────────────────────────────────────────────────┐
│                         main.py  (entry)                            │
│  1. create_connectors()       → instantiate all 6 platform objects  │
│  2. initialise_connectors()   → async init each (SDK, metadata)     │
│  3. ArbEngine(active_connectors)                                    │
│  4. engine.run_cycle()                                              │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ArbEngine.run_cycle()                             │
│                   (engine/arb_engine.py:55)                          │
│                                                                     │
│  Step 1 ──► aggregator.refresh_all_rates()                          │
│             Fetch funding rates from ALL platforms concurrently      │
│             Filter to TOP_200_SYMBOLS                                │
│                          │                                          │
│  Step 2 ──► _check_exits()                                          │
│             Close positions where spread ≤ exit threshold            │
│                          │                                          │
│  Step 3 ──► aggregator.find_opportunities()                         │
│             Scan for symbols with spread ≥ entry threshold           │
│             Sort by spread descending                                │
│                          │                                          │
│  Step 4 ──► _open_new_positions(opportunities)                      │
│             Run 9 constraint gates → execute order                   │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
    ┌──── CONSTRAINT GATES (Section 2 below) ────┐
    │  Gate 1–9 must ALL pass for a given         │
    │  opportunity before an order is placed       │
    └──────────────┬──────────────────────────────┘
                   │ (all gates passed)
                   ▼
    ┌──── POSITION MANAGER (Section 3 below) ─────┐
    │  Pre-flight re-checks → concurrent order     │
    │  execution → result handling → state save     │
    └──────────────┬──────────────────────────────┘
                   │
                   ▼
    ┌──── PLATFORM CONNECTOR (Section 4 below) ───┐
    │  Symbol lookup → mark price → size calc →    │
    │  DRY_RUN check → SDK order call              │
    └─────────────────────────────────────────────┘
```

---

## 2. CONSTRAINT GATES — `_open_new_positions()` (arb_engine.py:144–269)

Each opportunity is tested against these gates **in order**. If any gate fails, the opportunity is **skipped** and the next one is tried.

```
Opportunity (symbol, long_platform, short_platform, spread_ann)
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 1: Symbol Uniqueness                      (line 155)       │
│ Check:  No existing OPEN position for this symbol               │
│ Fails:  "Skipping {symbol}: already have an open position"      │
│ Verify: Check state/positions.json for this symbol              │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 2: Max Concurrent Positions               (line 160)       │
│ Check:  count_open < MAX_CONCURRENT_POSITIONS                   │
│ Config: MAX_CONCURRENT_POSITIONS  (default: 5)                  │
│ Fails:  "Max positions reached, stopping new opens" + BREAK     │
│ Note:   BREAKS loop — no more opportunities processed           │
│ Verify: Count OPEN entries in state/positions.json              │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 3: Connector Availability                 (line 164–173)   │
│ Check:  Both long_platform and short_platform have active       │
│         connectors (were successfully initialised)              │
│ Fails:  "Skipping {symbol}: missing connector for …"           │
│ Verify: Check startup logs for "Initialised: <platform>"        │
│         and "Failed to initialise <platform>"                   │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 4: Balance Fetch                          (line 176–183)   │
│ Check:  get_balance() succeeds on BOTH platforms                │
│ Fails:  "Skipping {symbol}: balance fetch failed: {error}"      │
│ Verify: Test each connector's get_balance() independently       │
│         Check API keys, wallet address, network connectivity    │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 5: Minimum Total Balance                  (line 185–191)   │
│ Check:  long_bal.equity_usd + short_bal.equity_usd              │
│         ≥ MIN_ACCOUNT_BALANCE_USD                               │
│ Config: MIN_ACCOUNT_BALANCE_USD  (default: $150.00)             │
│ Fails:  "Total balance ${X} below minimum ${Y}" + BREAK        │
│ Note:   BREAKS loop — no more opportunities processed           │
│ Verify: Sum equity on both platforms manually                   │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 6: Sufficient Free Margin                 (line 193–207)   │
│ Calc:   min_platform_bal = min(long.free_margin, short.free_m.) │
│         size_per_leg = min(                                     │
│           total_balance × (POSITION_SIZE_PCT / 100),            │
│           min_platform_bal × 0.95                               │
│         )                                                       │
│ Check:  size_per_leg > 0                                        │
│ Config: POSITION_SIZE_PCT  (default: 25%)                       │
│ Fails:  "Skipping {symbol}: insufficient margin …"             │
│ Verify: Check free_margin_usd on each platform                  │
│         Ensure funds are not locked in other positions           │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 7: Positive Net Daily Profit              (line 209–232)   │
│ Calc:   actual_daily = (spread_ann / 100 / 365) × size_per_leg │
│         total_fees = estimated round-trip fees (both legs)       │
│         net_daily = actual_daily − (total_fees / 30)            │
│ Check:  net_daily > 0                                           │
│ Fails:  "Skipping {symbol}: negative net profit …"             │
│ Verify: Compute spread × size manually                          │
│         Check fee estimates from each connector                  │
│         Ensure spread is large enough to cover amortised fees    │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 8: Minimum Monthly Profit Threshold       (line 234–240)   │
│ Check:  net_daily × 30 ≥ MIN_PROFIT_THRESHOLD_USD               │
│ Config: MIN_PROFIT_THRESHOLD_USD  (default: $0.50)              │
│ Fails:  "Skipping {symbol}: monthly profit ${X} below ${Y}"    │
│ Verify: Multiply net_daily from Gate 7 by 30                    │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE 9: Breakeven Window ≤ 7 Days              (line 242–249)   │
│ Calc:   days_to_breakeven = total_fees / net_daily              │
│ Check:  days_to_breakeven ≤ 7                                   │
│ Fails:  "Skipping {symbol}: breakeven in {X} days …"           │
│ Verify: Divide total_fees by net_daily_profit                   │
│         If > 7, fees are too high relative to the spread         │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        ▼  ALL GATES PASSED
              ┌─────────────────────┐
              │ open_arb_position() │
              └─────────────────────┘
```

---

## 3. POSITION OPENING SUB-FLOW — `open_arb_position()` (position_manager.py:143–252)

After all 9 gates pass, the position manager performs its own pre-flight checks and executes the trade.

```
open_arb_position(symbol, long_platform, short_platform, size_usd, entry_spread)
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ PRE-FLIGHT A: Symbol Uniqueness (re-check)       (line 156)     │
│ Same as Gate 1 — guards against race conditions                  │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ PRE-FLIGHT B: Max Positions (re-check)           (line 160)     │
│ Same as Gate 2                                                   │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ PRE-FLIGHT C: Connector Exists                   (line 164)     │
│ Both platform connectors must be present                         │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ PRE-FLIGHT D: Balance Requirements               (line 172)     │
│ check_balance_requirements(long, short, size_usd)                │
│ → For EACH platform:                                             │
│   - get_balance()                                                │
│   - free_margin_usd ≥ size_usd_per_leg                          │
│ Fails: "Insufficient margin on {platform}: ${free} < ${needed}" │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ EXECUTE: Concurrent Order Placement              (line 184)     │
│                                                                  │
│   long_conn.open_position(symbol, LONG, size_usd,               │
│       max_slippage_pct=MAX_SLIPPAGE_PCT)                         │
│                                    ← runs concurrently →         │
│   short_conn.open_position(symbol, SHORT, size_usd,             │
│       max_slippage_pct=MAX_SLIPPAGE_PCT)                         │
│                                                                  │
│ Config: MAX_SLIPPAGE_PCT  (default: 0.5%)                        │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ RESULT MATRIX                                    (line 197)     │
│                                                                  │
│  Both succeed  →  Create Position record, save to state file     │
│  Long OK, Short FAIL  →  UNWIND long leg (close_position)       │
│  Short OK, Long FAIL  →  UNWIND short leg (close_position)      │
│  Both fail     →  Return None, log error, no state change        │
│                                                                  │
│ Verify: Look at logs for "Failed to open arb for {symbol}"       │
│         and "Unwinding long/short leg on {platform}"             │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼ (both succeed)
┌──────────────────────────────────────────────────────────────────┐
│ STATE PERSISTENCE                                (line 244)     │
│ Save to:  state/positions.json                                   │
│ Data:     position id, symbol, platforms, sizes, spread,         │
│           entry_price, notional_usd, fees, order_ids, timestamp  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. PLATFORM CONNECTOR ORDER FLOW (per-platform)

Each connector's `open_position()` has its own internal checks before placing the trade.

### Generic Connector Flow (applies to all platforms):

```
open_position(symbol, side, size_usd, leverage=1.0, max_slippage_pct=0.5)
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ CHECK A: Symbol Exists on Platform                               │
│ Map canonical symbol (e.g. "BTC") to platform format             │
│ Fail: "Symbol {X} not available on {platform}"                   │
│ Verify: Check connector's symbol list after init                 │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ CHECK B: Fetch Mark Price                                        │
│ GET current mark/index price from platform API                   │
│ Fail: "Could not fetch mark price"                               │
│ Verify: Manually query the platform's price endpoint             │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ CHECK C: Size Calculation & Rounding                             │
│ size_base = size_usd / mark_price                                │
│ Round to platform's precision (szDecimals on HL)                 │
│ Check: size_base > 0 after rounding                              │
│ Fail: "Order size rounds to zero"                                │
│ Verify: Compute size_usd / price manually, check min order size  │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ CHECK D: DRY_RUN Guard                                           │
│ If DRY_RUN=true → log and return fake success (no real trade)    │
│ Config: DRY_RUN  (default: false)                                │
│ Verify: Ensure DRY_RUN=false in .env for real trades             │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ EXECUTE: Platform-Specific Order Submission                      │
│ (see platform table below)                                       │
└───────────────────────┬──────────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│ PARSE RESPONSE                                                   │
│ Success: Extract fill price, order ID → return TradeResult       │
│ Failure: "Order not filled: {status details}"                    │
│ Exception: Log error → return TradeResult(success=False)         │
└──────────────────────────────────────────────────────────────────┘
```

### Platform-Specific Details:

| Platform | Auth Method | Order Type | Slippage Control | Fee Estimate | Key Failure Modes |
|----------|-------------|------------|------------------|--------------|-------------------|
| **Hyperliquid** | EVM wallet signature (`eth_account`) | Market IOC (Immediate-or-Cancel) | `limit_px = mark × (1 ± slippage%)` | 3.5 bps/leg (taker) | SDK not installed; IOC not filled; symbol not in universe |
| **Drift** | Solana keypair | On-chain tx via Anchor | N/A (market) | 10 bps/leg | Solana RPC errors; insufficient SOL for tx fees; market index not found |
| **Lighter** | EVM private key | Market order via SDK | N/A | 5 bps/leg | SDK not installed; symbol mapping mismatch |
| **Ostium** | EVM private key + Arbitrum RPC | `open_trade()` with leverage/slippage | `slippage` param | 10 bps + rolling fees | Pair index not found; Arbitrum RPC failure |
| **Aster** | HMAC-SHA256 (API key/secret) | `MARKET` order via REST | N/A (market) | 4 bps/leg | API key invalid; insufficient margin; symbol not in exchangeInfo |
| **EdgeX** | HMAC-SHA256 (timestamp+path+body) | `MARKET` with limit price | `price` field as limit | 5 bps/leg | STARK key invalid; API signature mismatch |

---

## 5. EXIT FLOW

```
_check_exits()  (arb_engine.py:122)
    │
    ▼
For each OPEN position:
    │
    ├── Get current rates for position.long_platform & position.short_platform
    │   └── If either rate missing → skip (log warning)
    │
    ├── current_spread = (short_rate - long_rate) × 100
    │
    └── If current_spread ≤ EXIT_FUNDING_RATE_DIFF_PCT (default: 3.0%)
        │
        ▼
        close_arb_position()
        ├── Mark status = CLOSING
        ├── Close BOTH legs concurrently
        │   ├── long_conn.close_position(symbol, LONG, size)
        │   └── short_conn.close_position(symbol, SHORT, size)
        ├── Both succeed → status = CLOSED
        └── Either fails → status = FAILED
```

---

## 6. CONFIGURATION QUICK REFERENCE

| Variable | Default | Used At | Effect |
|----------|---------|---------|--------|
| `DRY_RUN` | `false` | Connector order execution | If `true`, no real trades — returns fake success |
| `ENTRY_FUNDING_RATE_DIFF_PCT` | `10.0` | Aggregator + Gate filter | Min annualised spread (%) to consider entry |
| `EXIT_FUNDING_RATE_DIFF_PCT` | `3.0` | Exit check | Spread (%) at or below which positions are closed |
| `MIN_ACCOUNT_BALANCE_USD` | `150.0` | Gate 5 | Min combined equity across both platforms |
| `POSITION_SIZE_PCT` | `25.0` | Gate 6 | % of total balance used per leg |
| `MAX_CONCURRENT_POSITIONS` | `5` | Gate 2 | Max simultaneous arb pairs |
| `MAX_SLIPPAGE_PCT` | `0.5` | Order execution | Slippage tolerance for limit price calc |
| `MIN_PROFIT_THRESHOLD_USD` | `0.5` | Gate 8 | Min acceptable monthly profit |
| `EVM_PRIVATE_KEY` | — | All EVM connectors | Wallet key for signing transactions |
| `EVM_PUBLIC_KEY` | — | Hyperliquid balance | Address for querying account state |
| `HYPERLIQUID_API_WALLET_KEY` | — | HL trading | API wallet key (if using delegated trading) |
| `SOLANA_PRIVATE_KEY` | — | Drift | Solana keypair for on-chain trades |
| `ASTER_API_KEY` / `_SECRET` | — | Aster | HMAC auth for ApolloX REST API |
| `EDGEX_API_KEY` / `_SECRET` / `_STARK_PRIVATE_KEY` | — | EdgeX | HMAC + STARK auth |

---

## 7. MANUAL DEBUGGING CHECKLIST

Work through these in order. Stop at the first failure — that's likely where the problem is.

### Stage A: Initialisation
- [ ] Bot starts without crash
- [ ] Each connector logs `"Initialised: {platform}"` — which ones are missing?
- [ ] `DRY_RUN` is set to `false` in `.env`
- [ ] Wallet keys are configured (`EVM_PRIVATE_KEY`, `EVM_PUBLIC_KEY`)
- [ ] Platform-specific API keys are set for target platforms

### Stage B: Funding Rate Fetch
- [ ] Logs show `"Fetched N rates from {platform}"` for each active connector
- [ ] At least 2 platforms return rates (need 2+ for a spread)
- [ ] Symbols are being normalised correctly (e.g. `"BTCUSDT"` → `"BTC"`)
- [ ] Rates are filtered to `TOP_200_SYMBOLS` — is your target symbol in the list?

### Stage C: Opportunity Detection
- [ ] Logs show `"Found N opportunities above X% threshold"`
- [ ] If 0 opportunities: check if any cross-platform spread ≥ `ENTRY_FUNDING_RATE_DIFF_PCT` (default 10%)
- [ ] **Common issue**: 10% annualised spread is a high bar. Consider lowering `ENTRY_FUNDING_RATE_DIFF_PCT`

### Stage D: Constraint Gates (in _open_new_positions)
- [ ] **Gate 1**: No duplicate symbol in `state/positions.json` with status `"open"`
- [ ] **Gate 2**: Fewer than `MAX_CONCURRENT_POSITIONS` (5) positions open
- [ ] **Gate 3**: Both platforms in the opportunity have active connectors
- [ ] **Gate 4**: `get_balance()` succeeds on both platforms (test manually)
- [ ] **Gate 5**: Combined equity ≥ `$150` (or your configured min)
- [ ] **Gate 6**: `free_margin_usd` > 0 on both platforms; `size_per_leg` computes > 0
- [ ] **Gate 7**: Net daily profit > 0 after fee amortisation
- [ ] **Gate 8**: Monthly profit ≥ `$0.50`
- [ ] **Gate 9**: Breakeven ≤ 7 days

### Stage E: Order Execution (inside connector)
- [ ] Symbol maps correctly to platform format (e.g. `"BTC"` → `"BTC"` on HL)
- [ ] Mark price fetched successfully (non-zero)
- [ ] `size_base = size_usd / mark_price` is > 0 after rounding
- [ ] For **Hyperliquid**: `hyperliquid-python-sdk` + `eth_account` are installed
- [ ] For **Drift**: `driftpy` + Solana deps installed, `SOLANA_PRIVATE_KEY` set
- [ ] For **Aster/EdgeX**: API keys valid, signatures generating correctly
- [ ] Order response contains `"filled"` status (not rejected/expired)
- [ ] If IOC order: check if liquidity was sufficient (order may expire unfilled)

### Stage F: Post-Execution
- [ ] Both legs returned `TradeResult(success=True)`
- [ ] If one leg failed: check logs for `"Unwinding"` — was the unwind successful?
- [ ] Position saved to `state/positions.json`
- [ ] Alert sent (if Telegram/Discord configured)

---

## 8. FILE REFERENCE

| File | Purpose |
|------|---------|
| `main.py` | Entry point, connector init, cycle orchestration |
| `config.py` | All settings loaded from `.env` |
| `engine/arb_engine.py` | Main cycle, 9 constraint gates, opportunity processing |
| `engine/aggregator.py` | Funding rate collection, spread calculation, opportunity finding |
| `engine/position_manager.py` | Position lifecycle: open, close, persist, balance checks |
| `connectors/base.py` | Abstract interface all connectors implement |
| `connectors/hyperliquid_conn.py` | Hyperliquid order placement |
| `connectors/drift_conn.py` | Drift (Solana) order placement |
| `connectors/lighter_conn.py` | Lighter order placement |
| `connectors/ostium_conn.py` | Ostium order placement |
| `connectors/aster_conn.py` | Aster (ApolloX) order placement |
| `connectors/edgex_conn.py` | EdgeX order placement |
| `utils/models.py` | TradeResult, Position, FundingRate, AccountBalance models |
| `utils/crypto_list.py` | TOP_200_SYMBOLS filter list |
| `state/positions.json` | Persisted position state (created at runtime) |
