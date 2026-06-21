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

## Safety rails (for a careful mainnet launch)
Set these so an untested bug can't drain the treasury. `0` = no cap.

| Var | Effect | Suggested first launch |
|-----|--------|------------------------|
| `MAX_LOAN_SOL` | Hard cap per single payout. Bigger quotes are **held**, not paid. | `0.1` |
| `MAX_OUTSTANDING_SOL` | Cap on total live credit across all loans. | `2` |
| `MANUAL_APPROVAL` | `1` = watcher detects the deposit but **waits for you** before paying. | `1` (first loans) |

Held / pending loans surface with `status: "held"` or `"pending_approval"`.
Release one with: `POST /api/loans/{id}/approve` → it pays on the next pass.

### Recommended "mainnet canary" first launch
1. New treasury wallet, fund with **0.3–0.5 SOL only**.
2. Variables: `RPC_URL`, `TREASURY_SECRET`, `MAX_LOAN_SOL=0.1`, `MAX_OUTSTANDING_SOL=2`, `MANUAL_APPROVAL=1`.
3. Do one loan yourself with a cheap bonded token; approve it; verify payout, repay, release, then force a liquidation.
4. Only then raise the caps / drop MANUAL_APPROVAL and fund the treasury for real.
With a small treasury + `MAX_LOAN_SOL`, the worst-case loss from a bug is bounded.
