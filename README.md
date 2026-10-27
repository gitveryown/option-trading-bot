# Options Desk

Version 0.4 connects the options bot to an Alpaca paper account. In broker mode, eligible signals submit day limit orders to the paper account, and positions change only when Alpaca reports confirmed fills. The dashboard shows durable order states, partial fills, positions, decision events and a strategy budget.

Real money execution is absent. The order transport uses the literal host `https://paper-api.alpaca.markets`. Environment variables cannot switch it to the live trading host.

## Start

Python 3.11 or newer is required. Broker mode currently supports macOS and Linux so the app can hold an account ownership lock. There are no Python runtime package dependencies.

1. Generate Alpaca paper API credentials and confirm options trading level 2 or higher.
2. Use a dedicated paper account with no unmanaged positions or open orders. The bot does not liquidate or cancel manual activity.
3. Copy .env.example to .env and replace the placeholders locally. Confirm OPRA options and SIP stock data access for those credentials.
4. Run the check command, then open the dashboard.

```bash
python3 -m options_bot check --execution alpaca_paper --db connection_probe.sqlite3
python3 -m options_bot dashboard
```

Open http://127.0.0.1:8765 and choose Connect Alpaca paper. The check command verifies credentials, account identity, options permission, a reconciled paper account and data request access. It submits no new orders. Rechecking an account with tracked pending orders can reconcile fills and request cancellation of orders due for cancellation.

The dashboard defaults to Alpaca paper execution and the separate alpaca_paper.sqlite3 database. Existing live_paper.sqlite3 local simulator accounts are not converted. The database records its execution backend and rejects opening in the wrong backend.

If you want the earlier simulator with actual quotes and no brokerage order submission:

```bash
python3 -m options_bot dashboard --execution local --db live_paper.sqlite3
```

Read QUICKSTART.md for credential setup and connection troubleshooting.

## Strategy budget and broker balance

The default strategy budget is $1,000, configurable from $1,000 to $5,000 on a new database. A broker paper account may have a larger balance. That does not increase this strategy's budget. The app does not reset the broker's cash balance or use real funds.

The dashboard's equity is the strategy budget ledger. It uses confirmed broker fill prices, last observed bids for open options, and modeled fees of $0.65 per contract on each side. Those fees are an experimental accounting assumption, not a statement of the broker's actual charges. This equity is different from total Alpaca account equity.

| Control | Default |
| :--- | :--- |
| Initial strategy budget | $1,000 |
| Full premium position budget | 2% of the smallest of initial budget, current equity, and session starting equity |
| Worst case reservation | Premium times 100, plus modeled entry and exit fees |
| Total exposure ceiling | 4%, including unfilled entry reservations |
| Daily loss threshold | 3%, including realized and marked unrealized losses |
| Account drawdown threshold | 10% from highest observed strategy equity |
| Simultaneous positions | One |
| Maximum entry attempts per session | Three, including rejected or canceled attempts |
| Consecutive losses | Three cause a session halt |
| Contracts | Standard SPY or QQQ long calls and puts |
| Expiration | 5 to 14 calendar days |
| Absolute delta | 0.35 to 0.65 |
| Open interest | At least 100 |
| Maximum spread | 10% of midpoint |
| Maximum quote age at submission | Ten seconds |
| Entry order | Limit at observed ask, day duration |
| Exit order | Limit at observed positive bid, day duration |
| Premium stop trigger | 20% below entry |
| Profit exit trigger | 40% above entry |
| Trailing exit | Activates at 20% gain; requests exit 15% below best observed bid |
| Maximum holding time | Sixty minutes |
| Entry window | Thirty minutes after open until thirty minutes before close |
| Session exit window | Fifteen minutes before close, including early closes |

The daily budget reserves the entire proposed premium against remaining loss capacity. The one position rule also blocks entries while an entry or exit order is still unresolved. If one contract will not fit, the bot skips it. A larger broker balance does not bypass these rules.

Thresholds request exits and do not guarantee a realized loss ceiling. Limit orders may not fill, data can become unavailable, and options can gap. Process downtime stops monitoring; orders already accepted by Alpaca may continue working. A position that cannot be exited before close can remain overnight.

## Signal rules

The strategy needs at least 25 consecutive completed underlying minute bars from session open. The latest bar must be no more than ninety seconds old.

1. Calls require a close above the prior five bar highs and session VWAP, EMA9 above EMA21, rolling RSI14 from 55 to 80, and volume at least 1.3 times the previous twenty bar average.
2. Puts require a close below the prior five bar lows and VWAP, EMA9 below EMA21, RSI14 from 20 to 45, and the same volume condition.
3. Contract filters enforce direction, expiration, delta, open interest, spread, data freshness and affordability before an order intent can exist.

RSI uses a simple rolling average, not Wilder smoothing. EMA seeds from the first session close. The data scan covers strikes within 3% of the last underlying close and refreshes up to forty candidate contracts. This is a bounded experimental scan, not exhaustive market coverage. Held contracts are requested explicitly, even outside that scan.

There is no AI profitability score or proven return estimate. A CLI reporting goal of $2,000 does not change trade selection or risk rules.

## Broker order lifecycle

1. A qualified signal creates a durable order intent with a deterministic client order id.
2. The account, positions and open orders are reconciled before orders are allowed. The broker clock is checked again immediately before submission.
3. The database records the submission attempt before the POST request.
4. An accepted order remains pending. It does not create a position until confirmed filled quantity increases.
5. Cumulative partial fills update the ledger only for newly confirmed quantity and notional. Repeated confirmations have no additional accounting effect.
6. A submission timeout causes a halt. Reconnection looks up the same client id instead of blindly repeating the POST.
7. An unfilled entry is requested canceled after sixty seconds. An unfilled exit is requested canceled after thirty seconds so a later current quote can price a new exit. Replacement waits for terminal cancellation confirmation.

Cancellation requests do not prove cancellation. Pending buy fills are reconciled even after a halt, and remaining entry quantity must reach a terminal state before a closing order is prepared. The bot never submits sell to open orders and does not assume broker native bracket protection.

A computer local account lock prevents two running processes here from controlling the same account. It does not coordinate with another computer or external trading service. Use one dedicated account and one running bot. Unmanaged orders or position mismatches cause a persistent halt and require inspection in Alpaca.

## Dashboard and controls

The dashboard binds to 127.0.0.1. It shows quotes, strategy budget equity, positions, the latest thirty order intents and receipts, the latest one hundred decision events, and observed equity history. Historical chart points come from processed frames and are not fabricated.

Controls require a local Host, a local Origin when present, and a request token. The server does not expose arbitrary paths or credential files. This is a local interface, not a public hosted service.

Run test uses a separate local simulator account with explicit synthetic labeling. It cannot place Alpaca orders. It cannot replace the active account view while a quote worker is running.

Halt and request exits blocks entries, requests cancellation of tracked entry orders, and leaves the quote worker collecting prices needed for exit orders. Halts persist after restart and are not cleared by reconnecting.

```bash
python3 -m options_bot status --execution alpaca_paper
python3 -m options_bot halt --execution alpaca_paper
```

Keep the worker running to process confirmations and exits. Control+C requests a halt and attempts reconciliation before stopping. It cannot guarantee cancellation or liquidation before exit. Inspect the Alpaca paper dashboard if a connection failure prevents confirmation. Do not delete an account database to resolve an unknown order outcome.

## Terminal runner and local replay

```bash
bash start.sh
python3 -m options_bot live --execution alpaca_paper --once
python3 -m options_bot demo --scenario mechanics --db sample.sqlite3
python3 -m options_bot replay --input observed_quotes.jsonl --db replay.sqlite3 --report replay.json
```

Demo and replay use the local simulator only. Historical or synthetic frames cannot enter the broker engine. Fresh market frames are recorded in observed_quotes.jsonl; the broker ledger and durable intents remain in alpaca_paper.sqlite3.

The included synthetic mechanics fixture ends at $1,005.70, the price gap fixture at $982.70, and the unaffordable contract fixture at $1,000.00. Their prices are contrived to test accounting. They provide no market performance evidence.

## Implementation

| File | Responsibility |
| :--- | :--- |
| options_bot/alpaca_paper.py | Fixed paper host, account validation, order submission, lookup and cancellation |
| options_bot/paper_engine.py | Durable intents, confirmed fills, reconciliation and broker execution decisions |
| options_bot/execution.py | Explicit selection of local or broker paper execution |
| options_bot/engine.py | Original local simulator and shared account risk calculations |
| options_bot/alpaca_data.py | OPRA snapshots, SIP bars, contract catalog and exchange calendar |
| options_bot/dashboard.py | Local server, worker lifecycle, account views and controls |
| options_bot/dashboard.html | Interface for budget, quotes, orders, positions and journal |
| options_bot/store.py | Atomic SQLite checkpoints, processed frames and audit events |
| options_bot/strategy.py | Momentum indicators and breakout signals |

## Validation and limits

89 automated checks pass: 29 original engine checks, 14 mocked market data checks, 19 dashboard checks and 27 paper transport and execution checks. They cover partial fills, accepted but unfilled orders, rejections, ambiguous timeouts, restart reconciliation, account identity, ownership locks, cancellation, backend separation and budget enforcement.

The tests use mocked broker and market responses. No authenticated Alpaca account or real paper order has been tested in this workspace. Browser visual review remains outstanding; JavaScript syntax was checked. Configure your credentials locally, verify access, and supervise an initial paper session.

This release uses REST polling. It does not implement a trading event stream, exercise instructions, broker reported fee adjustments or a remote monitoring service. Manual trading, exercise or assignment that changes expected positions causes a halt rather than an invented ledger adjustment.

Protocol references:

1. [Create a paper order](https://docs.alpaca.markets/us/reference/postorder)
2. [Lookup by client order id](https://docs.alpaca.markets/us/reference/getorderbyclientorderid)
3. [Options trading permissions](https://docs.alpaca.markets/us/docs/options-trading)
4. [Option chain](https://docs.alpaca.markets/us/reference/optionchain)
