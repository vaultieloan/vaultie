"""
Vaultie custodial lending engine
================================
Moves real funds ONLY when armed via environment variables:

    RPC_URL          Solana RPC (e.g. Helius mainnet URL with ?api-key=...)
    TREASURY_SECRET  Treasury private key (base58 string OR JSON byte array)
    VAULTIE_MINT     $VAULTIE mint (optional; enables stake-boost / discount)
    ENGINE_ENABLED   "0" to fully disable the background loop

Safety model
------------
* If RPC_URL or TREASURY_SECRET is missing  ->  DRY_RUN: every money action is
  logged and skipped, nothing leaves the wallet.
* Idempotent: a loan is only paid out / liquidated once (guarded by stored sigs).
* Treasury-balance guard before every payout.
* NEVER hardcode keys here. Set them as host secrets (Railway -> Variables).
* TEST ON DEVNET WITH TINY AMOUNTS before pointing at mainnet with real float.
"""
import os, json, base64, logging

log = logging.getLogger("vaultie.engine")
logging.basicConfig(level=logging.INFO)

RPC_URL         = os.getenv("RPC_URL", "").strip()
TREASURY_SECRET = os.getenv("TREASURY_SECRET", "").strip()
VAULTIE_MINT    = os.getenv("VAULTIE_MINT", "").strip()
ENGINE_ENABLED  = os.getenv("ENGINE_ENABLED", "1") != "0"

# ---- safety rails (0 = no cap) ----
MAX_LOAN_SOL        = float(os.getenv("MAX_LOAN_SOL", "0"))         # hard cap per single payout
MAX_OUTSTANDING_SOL = float(os.getenv("MAX_OUTSTANDING_SOL", "0"))  # cap on total live credit
MAX_PER_WALLET_SOL  = float(os.getenv("MAX_PER_WALLET_SOL", "0"))   # cap on one wallet's total live credit
LOCK_GAS_SOL        = float(os.getenv("LOCK_GAS_SOL", "0.01"))      # SOL sent to each lock address to fund release/liquidation fees
MANUAL_APPROVAL     = os.getenv("MANUAL_APPROVAL", "0") == "1"      # require operator OK before payout
LIQUIDATE_ALL       = os.getenv("LIQUIDATE_ALL", "0") == "1"        # EMERGENCY: sell ALL collateral -> sweep to treasury, stop new payouts
WITHDRAW_ALL        = os.getenv("WITHDRAW_ALL", "0") == "1"         # EMERGENCY: move ALL locked tokens to the treasury wallet, stop new payouts

LAMPORTS = 1_000_000_000
WSOL     = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP  = "https://quote-api.jup.ag/v6/swap"


def _load_keypair(secret: str):
    from solders.keypair import Keypair
    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


class Engine:
    """Thin, defensive wrapper around solana-py. Degrades to dry-run on any gap."""

    def __init__(self):
        self.ready = False         # libs + RPC present
        self.dry = True            # no treasury key -> simulate
        self.reason = ""
        self.client = None
        self.treasury = None
        try:
            from solana.rpc.api import Client
        except Exception as e:                       # libs not installed
            self.reason = f"solana lib unavailable: {e}"
            log.warning("engine: %s", self.reason)
            return
        if not RPC_URL:
            self.reason = "RPC_URL not set"
            log.info("engine: %s (dry-run)", self.reason)
            return
        try:
            self.client = Client(RPC_URL)
        except Exception as e:
            self.reason = f"RPC init failed: {e}"
            return
        if TREASURY_SECRET:
            try:
                self.treasury = _load_keypair(TREASURY_SECRET)
                self.dry = False
                log.info("engine: ARMED, treasury=%s", str(self.treasury.pubkey()))
            except Exception as e:
                self.reason = f"bad TREASURY_SECRET: {e}"
                log.error("engine: %s", self.reason)
                return
        else:
            self.reason = "TREASURY_SECRET not set (read-only / dry-run)"
            log.info("engine: %s", self.reason)
        self.ready = True

    # ---------- reads ----------
    def _pk(self, s):
        from solders.pubkey import Pubkey
        return Pubkey.from_string(s)

    def sol_balance(self, addr: str) -> float:
        try:
            v = self.client.get_balance(self._pk(addr)).value
            return v / LAMPORTS
        except Exception as e:
            log.warning("sol_balance(%s): %s", addr, e); return 0.0

    def token_balance(self, owner: str, mint: str):
        """Return (ui_amount, decimals) of `mint` held by `owner`."""
        try:
            from solana.rpc.types import TokenAccountOpts
            r = self.client.get_token_accounts_by_owner_json_parsed(
                self._pk(owner), TokenAccountOpts(mint=self._pk(mint)))
            ui, dec = 0.0, 0
            for acc in r.value:
                info = acc.account.data.parsed["info"]["tokenAmount"]
                ui += float(info["uiAmount"] or 0)
                dec = int(info["decimals"])
            return ui, dec
        except Exception as e:
            log.warning("token_balance(%s,%s): %s", owner, mint, e); return 0.0, 0

    def vaultie_balance(self, owner: str) -> float:
        if not VAULTIE_MINT:
            return 0.0
        return self.token_balance(owner, VAULTIE_MINT)[0]

    # ---------- writes ----------
    def _send_ixs(self, payer_kp, ixs):
        """Compile + send a list of instructions as a versioned tx signed by payer_kp."""
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        bh = self.client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(payer_kp.pubkey(), ixs, [], bh)
        tx = VersionedTransaction(msg, [payer_kp])
        return self.client.send_raw_transaction(bytes(tx)).value

    def send_sol(self, to: str, sol: float) -> str:
        """Pay `sol` from treasury to `to`. Returns tx signature or 'DRYRUN'/'ERR'."""
        lamports = int(round(sol * LAMPORTS))
        if self.dry or not self.treasury:
            log.info("[DRY] send_sol -> %s : %.6f SOL", to, sol); return "DRYRUN"
        try:
            from solders.system_program import transfer, TransferParams
            ix = transfer(TransferParams(
                from_pubkey=self.treasury.pubkey(), to_pubkey=self._pk(to), lamports=lamports))
            sig = self._send_ixs(self.treasury, [ix])
            log.info("send_sol -> %s : %.6f SOL : %s", to, sol, sig)
            return str(sig)
        except Exception as e:
            log.error("send_sol failed: %s", e); return "ERR:" + str(e)[:60]

    def detect_sender(self, lock_address: str, mint: str):
        """Find the wallet that sent `mint` to lock_address, via tx history + balance deltas."""
        if self.dry:
            return None
        try:
            sigs = self.client.get_signatures_for_address(self._pk(lock_address), limit=15).value
            for s in sigs:
                if getattr(s, "err", None):
                    continue
                tx = self.client.get_transaction(s.signature, max_supported_transaction_version=0).value
                meta = tx.transaction.meta if (tx and tx.transaction) else None
                if not meta:
                    continue
                pre, post = {}, {}
                for b in (meta.pre_token_balances or []):
                    if str(b.mint) == str(mint):
                        pre[str(b.owner)] = float(b.ui_token_amount.ui_amount or 0)
                for b in (meta.post_token_balances or []):
                    if str(b.mint) == str(mint):
                        post[str(b.owner)] = float(b.ui_token_amount.ui_amount or 0)
                best, best_drop = None, 0.0
                for owner in set(list(pre.keys()) + list(post.keys())):
                    if owner == str(lock_address):
                        continue
                    drop = pre.get(owner, 0.0) - post.get(owner, 0.0)
                    if drop > best_drop:
                        best, best_drop = owner, drop
                if best and best_drop > 0:
                    log.info("detect_sender(%s): %s", lock_address[:6], best[:6])
                    return best
        except Exception as e:
            log.warning("detect_sender(%s): %s", lock_address, e)
        return None

    def sweep_sol(self, secret: str, to: str) -> str:
        """Send the full SOL balance (minus fee) from `secret`'s wallet to `to` (treasury)."""
        if self.dry or not secret:
            log.info("[DRY] sweep -> %s", to); return "DRYRUN"
        try:
            from solders.system_program import transfer, TransferParams
            kp = _load_keypair(secret)
            lamports = int(round(self.sol_balance(str(kp.pubkey())) * LAMPORTS)) - 5000
            if lamports <= 0:
                return "ERR:empty"
            ix = transfer(TransferParams(
                from_pubkey=kp.pubkey(), to_pubkey=self._pk(to), lamports=lamports))
            sig = self._send_ixs(kp, [ix])
            log.info("sweep -> %s : %d lamports : %s", to, lamports, sig); return str(sig)
        except Exception as e:
            log.error("sweep failed: %s", e); return "ERR:" + str(e)[:60]

    def release_collateral(self, lock_secret: str, to: str, mint: str,
                           ui_amount: float) -> str:
        """Send the locked SPL tokens from a lock address back to the borrower."""
        if self.dry or not lock_secret:
            log.info("[DRY] release %.4f of %s -> %s", ui_amount, mint, to); return "DRYRUN"
        try:
            from spl.token.instructions import (
                transfer_checked, TransferCheckedParams, get_associated_token_address,
                create_associated_token_account)
            from spl.token.constants import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID
            lock_kp = _load_keypair(lock_secret)
            mint_pk = self._pk(mint)
            # detect which token program owns the mint (classic SPL vs Token-2022)
            prog = TOKEN_PROGRAM_ID
            try:
                owner_prog = self.client.get_account_info(mint_pk).value.owner
                if str(owner_prog) == str(TOKEN_2022_PROGRAM_ID):
                    prog = TOKEN_2022_PROGRAM_ID
            except Exception:
                pass
            _, dec = self.token_balance(str(lock_kp.pubkey()), mint)
            src = get_associated_token_address(lock_kp.pubkey(), mint_pk, prog)
            dst = get_associated_token_address(self._pk(to), mint_pk, prog)
            ixs = []
            # create destination ATA if missing (payer = lock wallet)
            if not self.client.get_account_info(dst).value:
                ixs.append(create_associated_token_account(lock_kp.pubkey(), self._pk(to), mint_pk, prog))
            ixs.append(transfer_checked(TransferCheckedParams(
                program_id=prog, source=src, mint=mint_pk, dest=dst,
                owner=lock_kp.pubkey(), amount=int(round(ui_amount * (10 ** dec))), decimals=dec)))
            sig = self._send_ixs(lock_kp, ixs)
            log.info("release_collateral -> %s (prog=%s) : %s", to, str(prog)[:4], sig); return str(sig)
        except Exception as e:
            log.error("release_collateral failed: %s", e); return "ERR:" + str(e)[:90]

    def liquidate_swap(self, lock_secret: str, mint: str, ui_amount: float) -> str:
        """Sell locked collateral for SOL via Jupiter (lock wallet signs)."""
        if self.dry or not lock_secret:
            log.info("[DRY] liquidate %.4f of %s -> SOL", ui_amount, mint); return "DRYRUN"
        try:
            import httpx
            from solders.transaction import VersionedTransaction
            from solders.keypair import Keypair
            lock_kp = _load_keypair(lock_secret)
            _, dec = self.token_balance(str(lock_kp.pubkey()), mint)
            amount = int(round(ui_amount * (10 ** dec)))
            q = httpx.get(JUP_QUOTE, params={
                "inputMint": mint, "outputMint": WSOL, "amount": amount,
                "slippageBps": 300}, timeout=20).json()
            swap = httpx.post(JUP_SWAP, json={
                "quoteResponse": q, "userPublicKey": str(lock_kp.pubkey()),
                "wrapAndUnwrapSol": True}, timeout=20).json()
            raw = base64.b64decode(swap["swapTransaction"])
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [lock_kp])
            sig = self.client.send_raw_transaction(bytes(signed)).value
            log.info("liquidate_swap %s : %s", mint, sig); return str(sig)
        except Exception as e:
            log.error("liquidate_swap failed: %s", e); return "ERR:" + str(e)[:60]


ENGINE = Engine()


def status() -> dict:
    return {
        "ready": ENGINE.ready, "dryRun": ENGINE.dry, "enabled": ENGINE_ENABLED,
        "reason": ENGINE.reason, "vaultieMint": bool(VAULTIE_MINT),
        "treasurySet": ENGINE.treasury is not None,
        "manualApproval": MANUAL_APPROVAL,
    }
