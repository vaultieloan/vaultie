# Vaultie engine — how to arm it

The lending engine (`engine.py`) moves real funds **only** when these env vars are
set on the backend host (Railway → Variables). Without them it runs in **DRY-RUN**:
every payout / release / liquidation is logged and skipped — nothing leaves a wallet.

## Environment variables
| Var | What | Required to go live |
|-----|------|---------------------|
| `RPC_URL` | Solana RPC, e.g. `https://mainnet.helius-rpc.com/?api-key=…` | yes |
| `TREASURY_SECRET` | Treasury private key — base58 string **or** JSON byte array | yes (to pay) |
| `VAULTIE_MINT` | $VAULTIE mint — enables stake-boost / discount | optional |
| `ENGINE_ENABLED` | `0` disables the background loop | optional |
| `ENGINE_POLL` | seconds between watcher passes (default 20) | optional |
| `DATA_DIR` | persistent dir for the JSON DB (Railway volume, e.g. `/data`) | recommended |

## What the engine does each pass
1. **Deposit watcher** — for loans `awaiting_deposit`, checks the lock address on-chain;
   once the collateral has arrived it reads spot TOKEN/SOL, applies the $VAULTIE boost,
   guards the treasury balance, and **sends the SOL credit** to the borrower. Idempotent.
2. **Liquidation** — for `active` loans, if spot ≤ liquidation price it sells the
   collateral for SOL via Jupiter (lock wallet signs).
3. **Repay** → releases the collateral SPL tokens back to the borrower.

## SAFETY — read before mainnet
* **Rotate the Helius key** if it was ever pasted anywhere; treat exposed keys as burned.
* `TREASURY_SECRET` is a **hot key**. Use a dedicated wallet, fund it with only as much
  SOL as you're willing to lend, set it as a Railway **secret**, never commit it.
* **Test on devnet first** (`RPC_URL` = a devnet endpoint, tiny amounts) end-to-end:
  open → send collateral → see payout → repay → see release → force a liquidation.
* Custodial: you are the counterparty. Fast liquidation + conservative LTV/Smart-Cap
  protect the treasury, not the token's market price.
* `GET /api/engine/status` shows `{ready, dryRun, reason, treasury}` so you can confirm
  the engine is armed correctly without exposing the key.
