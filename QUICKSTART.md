# Connect Alpaca paper trading

Version 0.4 can place and manage orders in your Alpaca paper account. Real money execution is disabled. Python 3.11 or newer on macOS or Linux is required for broker mode.

## 1. Prepare your paper account

Generate Alpaca paper API keys. Use a dedicated paper account with no other open orders or positions. Confirm options trading level 2 or higher and current OPRA options plus SIP stock data access.

The app does not reset your broker balance. It uses a separate $1,000 strategy budget even if the paper account has more simulated cash. No real funding is needed by the paper execution code.

## 2. Enter credentials locally

Extract the project and open a terminal inside options_bot. If .env does not already exist:

```bash
cp .env.example .env
```

Edit .env in your editor:

```text
APCA_API_KEY_ID=YOUR_PAPER_API_KEY
APCA_API_SECRET_KEY=YOUR_PAPER_API_SECRET
```

Use paper keys and keep them private. Do not paste them into chat. There is no setting to choose the live trading host.

## 3. Verify access

```bash
python3 -m options_bot check --execution alpaca_paper --db connection_probe.sqlite3
```

The check reads account identity, options permissions, open positions and orders, and tests OPRA and SIP data requests. It does not create a new trade. If the paper account is not flat or has unmanaged orders, resolve those directly in Alpaca before proceeding with a fresh strategy database.

A request accepted outside market hours does not mean current quotes are available. The app waits for the exchange session instead of substituting synthetic quotes.

## 4. Launch

```bash
python3 -m options_bot dashboard
```

Open http://127.0.0.1:8765 and choose Connect Alpaca paper. A successful connection starts automatic scanning during market hours. Eligible signals can submit paper limit orders. They appear in the dashboard's Alpaca paper orders table and in your Alpaca paper account.

The strategy can take no trades if contracts are too expensive or signals do not qualify. The default full premium budget is approximately $20 per position. An accepted order is not shown as a filled position until Alpaca confirms a fill.

The terminal alternative is:

```bash
bash start.sh
```

Use one running instance per paper account. The ownership lock applies on this computer, not across multiple computers.

## Account files

1. alpaca_paper.sqlite3 contains the broker confirmed strategy ledger, durable order intents and halt state.
2. observed_quotes.jsonl contains observed market input for local replay.
3. paper_runs contains isolated synthetic demonstration accounts.

The earlier live_paper.sqlite3 simulator database is retained separately. A database cannot be reused across execution backends.

```bash
python3 -m options_bot status --execution alpaca_paper
python3 -m options_bot halt --execution alpaca_paper
```

Keep the worker running after a halt so it can cancel entry orders, process any fills that race cancellation, and submit closing limits when fresh quotes are available. Cancellation and exit limits can remain pending.

Control+C stops monitoring and requests a persistent halt. Already accepted orders can continue working at Alpaca. If an API failure prevents confirmation, inspect the broker paper dashboard. Restart the same strategy database to reconcile. A restart never blindly repeats an unknown submission or clears a safety halt.

Do not delete an account database that has an open position, pending order or unknown submission outcome. The database is how the bot identifies its broker orders.

## Common messages

| Message | Meaning |
| :--- | :--- |
| Missing credentials | Add the paper key and secret to .env |
| HTTP 401 | Verify the credential pair is active |
| HTTP 403 from market data | Verify OPRA and SIP access |
| Options level 2 required | Enable the required paper account options permissions |
| Another bot controls the account | Stop the other process and reconcile one instance |
| Unmanaged positions or orders | Inspect Alpaca; the app will not modify manual activity |
| Order outcome unknown | Inspect the matching client order id in Alpaca; do not create another database or resubmit manually |
| Database belongs to another backend | Choose the correct execution backend and its original database |
| Market closed | The app waits for an open session |

## What has been verified

89 automated tests pass using local HTTP and mocked broker responses. They cover the original strategy and risk engine, real data field mapping, broker order states, partial fills, timeout handling, restarts and dashboard integration. JavaScript syntax was checked. Browser visual review and a real authenticated Alpaca paper session are still outstanding.

Actual account connection cannot occur here until credentials are available locally on the computer running the app. Do not interpret passing mocked tests as evidence of profitable trading.
