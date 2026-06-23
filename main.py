"""
Vaultie Protocol — custodial lending backend (MVP).

Honest scope: this service manages lock addresses and an off-chain treasury.
It is custodial. There is no on-chain enforcement. Operators must add real
custody, key management, settlement and monitoring before handling user funds.

Endpoints (all under /api):
  GET  /protocol/stats
  GET  /tokens
  GET  /tokens/lookup?address=
  POST /loans/quote
  POST /loans
  GET  /loans?recipient=
  POST /loans/{loan_id}/repay
  POST /staking/sol
  POST /staking/token
"""
import os, json, time, uuid, secrets, logging
from pathlib import Path
import engine
log = logging.getLogger("vaultie")
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- configurable starting parameters ----
LTV          = float(os.getenv("LTV", "0.10"))
LTV_BOOST    = float(os.getenv("LTV_BOOST", "0.15"))
INTEREST     = float(os.getenv("INTEREST", "0.05"))

# ---- loan terms: shorter = cheaper, longer = pricier. key:seconds:interest ----
def _parse_terms(raw):
    out = []
    labels = {"2h": "2 hours", "1d": "1 day", "1w": "1 week", "1mo": "1 month"}
    for part in raw.split(","):
        b = part.split(":")
        if len(b) == 3:
            out.append({"key": b[0], "label": labels.get(b[0], b[0]),
                        "seconds": int(b[1]), "interest": float(b[2])})
    return out
LOAN_TERMS = _parse_terms(os.getenv("LOAN_TERMS", "2h:7200:0.02,1d:86400:0.04,1w:604800:0.07,1mo:2592000:0.12"))
DEFAULT_TERM = os.getenv("DEFAULT_TERM", "1w")

def term_for(key):
    for t in LOAN_TERMS:
        if t["key"] == key:
            return t
    for t in LOAN_TERMS:
        if t["key"] == DEFAULT_TERM:
            return t
    return LOAN_TERMS[0] if LOAN_TERMS else {"key": "1w", "label": "1 week", "seconds": 604800, "interest": INTEREST}
LIQ_DROP     = float(os.getenv("LIQ_DROP", "0.50"))
CAP          = float(os.getenv("SMART_CAP", "0.10"))
BOOST_MIN    = int(os.getenv("BOOST_MIN", "10000000"))
SOL_USD      = float(os.getenv("SOL_USD", "165"))          # fallback only; live pairs derive SOL price from Dexscreener

# ---- $VAULTIE stake → LTV tiers (+5% steps) + repayment reputation ----
def _parse_tiers(raw):
    out = []
    for part in raw.split(","):
        if ":" in part:
            thr, val = part.split(":"); out.append((float(thr), float(val)))
    return sorted(out)
# threshold($VAULTIE held) : LTV  — default steps of +5%
LTV_TIERS   = _parse_tiers(os.getenv("LTV_TIERS", "10000000:0.15,50000000:0.20,100000000:0.25"))
LTV_MAX     = float(os.getenv("LTV_MAX", "0.25"))       # hard ceiling on effective LTV
REP_PER_SOL = float(os.getenv("REP_PER_SOL", "0.002"))  # +LTV per 1 SOL of repaid credit
REP_MAX     = float(os.getenv("REP_MAX", "0.05"))       # cap on the reputation bonus
DATA_DIR     = Path(os.getenv("DATA_DIR", "./data"))
ORIGINS      = os.getenv("CORS_ORIGINS", "*").split(",")

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "vaultie.json"

# ---- curated watchlist (addresses only; all data pulled live from Dexscreener) ----
SEED_TOKENS = [
    {"address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"},  # WIF
    {"address": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"},  # POPCAT
    {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},  # BONK
    {"address": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5"},   # MEW
    {"address": "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC"},   # FARTCOIN
    {"address": "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9"},  # GIGA
]


def _load():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {"loans": [], "lp": [], "stakes": {}}


def _save(db):
    DB_FILE.write_text(json.dumps(db, indent=2))


def new_lock_address() -> dict:
    """Generate a fresh Solana lock address + keep its secret server-side.
    Falls back to a clearly-marked placeholder if crypto libs are absent."""
    try:
        import base58
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        pub = bytes(sk.verify_key)
        secret = bytes(sk) + pub  # 64-byte solana secret key
        return {"address": base58.b58encode(pub).decode(),
                "secret": base58.b58encode(secret).decode()}
    except Exception:
        return {"address": "LOCK" + secrets.token_hex(20), "secret": None, "placeholder": True}


app = FastAPI(title="Vaultie Protocol API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---------- live market data (Dexscreener, no key required) ----------
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = float(os.getenv("PRICE_TTL", "30"))


def _parse_dex(payload: dict, address: str) -> Optional[dict]:
    pairs = [p for p in (payload.get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    # pick the deepest pool
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    price_usd = float(p.get("priceUsd") or 0)
    price_sol = float(p.get("priceNative") or 0)          # token price denominated in SOL
    liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
    sol_usd = (price_usd / price_sol) if price_sol else SOL_USD
    base = p.get("baseToken") or {}
    return {
        "symbol": base.get("symbol") or address[:4].upper(),
        "name": base.get("name") or "Pump.fun token",
        "address": address,
        "priceUsd": price_usd,
        "priceSol": price_sol,
        "liquidityUsd": liq_usd,
        "liquiditySol": (liq_usd / sol_usd) if sol_usd else 0,
        "solUsd": sol_usd,
        "change24h": float((p.get("priceChange") or {}).get("h24") or 0),
        "live": True,
    }


def fetch_token(address: str) -> Optional[dict]:
    """Live token data with a short cache; returns None on failure."""
    hit = _CACHE.get(address)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    try:
        import httpx
        r = httpx.get(DEX_URL + address, timeout=4.0,
                      headers={"User-Agent": "vaultie/0.1"})
        r.raise_for_status()
        data = _parse_dex(r.json(), address)
        if data:
            _CACHE[address] = (time.time(), data)
        return data
    except Exception:
        return None


def get_token(address: str) -> Optional[dict]:
    """Live data only. Returns None if the token has no resolvable Solana market."""
    return fetch_token(address)


def stake_ltv(vaultie_balance: float) -> float:
    """LTV from $VAULTIE held: walks the tiers, highest matched wins."""
    ltv = LTV
    for thr, val in LTV_TIERS:
        if vaultie_balance >= thr:
            ltv = val
    return ltv

def repaid_volume(db, wallet: str) -> float:
    """Total SOL credit this wallet has successfully repaid (reputation basis)."""
    if not wallet:
        return 0.0
    return sum(l.get("creditSol", 0) for l in db["loans"]
               if l["status"] == "repaid" and (l.get("fromWallet") or l.get("recipient")) == wallet)

def reputation_bonus(vol_sol: float) -> float:
    return min(REP_MAX, vol_sol * REP_PER_SOL)

def effective_ltv(vaultie_balance: float, repaid_vol: float) -> float:
    """Base + stake tier + reputation, capped at LTV_MAX."""
    return round(min(LTV_MAX, stake_ltv(vaultie_balance) + reputation_bonus(repaid_vol)), 6)


def appraise(token, amount, boost, interest=None):
    """Appraise against the live spot price. Works in SOL directly via priceSol."""
    if interest is None:
        interest = INTEREST
    ltv = LTV_BOOST if boost else LTV
    value_usd = amount * token["priceUsd"]
    value_sol = amount * token["priceSol"]
    credit_sol = value_sol * ltv
    interest_sol = credit_sol * interest
    liq_price_sol = token["priceSol"] * (1 - LIQ_DROP)     # -50% from entry
    cap_credit_sol = token["liquiditySol"] * CAP * ltv      # Smart Cap drawable
    return {
        "appraisedUsd": round(value_usd, 2),
        "appraisedSol": round(value_sol, 4),
        "ltv": ltv,
        "interest": interest,
        "creditSol": round(credit_sol, 6),
        "interestSol": round(interest_sol, 6),
        "repaySol": round(credit_sol + interest_sol, 6),
        "liqPriceSol": liq_price_sol,
        "maxCreditSol": round(cap_credit_sol, 4),
        "overCap": credit_sol > cap_credit_sol,
        "live": token.get("live", False),
    }


# ---------- schemas ----------
class QuoteIn(BaseModel):
    tokenAddress: str
    amount: float
    boost: bool = False
    term: Optional[str] = None        # term key: 2h / 1d / 1w / 1mo

class LoanIn(QuoteIn):
    symbol: Optional[str] = None
    recipient: Optional[str] = None   # not required: credit returns to the sending wallet

class ConfirmIn(BaseModel):
    fromWallet: Optional[str] = None  # wallet the deposit was sent from (optional hint)

class StakeIn(BaseModel):
    amount: float
    recipient: str


# ---------- routes ----------
@app.get("/api/protocol/locked")
def locked():
    """Currently-locked collateral, aggregated per token. Real locks only (collateral present)."""
    db = _load()
    agg = {}
    for l in db["loans"]:
        if l["status"] not in ("active", "awaiting_repayment"):
            continue
        a = l["tokenAddress"]
        if a not in agg:
            agg[a] = {"symbol": l.get("symbol", "?"), "tokenAddress": a,
                      "amount": 0.0, "positions": 0, "creditSol": 0.0}
        agg[a]["amount"] += l.get("amount", 0) or 0
        agg[a]["positions"] += 1
        agg[a]["creditSol"] += l.get("creditSol", 0) or 0
    out = sorted(agg.values(), key=lambda x: -x["creditSol"])
    return {"locked": [{"symbol": x["symbol"], "tokenAddress": x["tokenAddress"],
                        "amount": round(x["amount"], 4), "positions": x["positions"],
                        "creditSol": round(x["creditSol"], 6)} for x in out]}


@app.get("/api/protocol/stats")
def stats():
    db = _load()
    loans = db["loans"]
    active = [l for l in loans if l["status"] == "active"]
    repaid = [l for l in loans if l["status"] == "repaid"]
    liquidated = [l for l in loans if l["status"] in ("liquidated", "defaulted", "force_liquidated")]
    funded = [l for l in loans if l.get("disburseSig") or l["status"] in ("active", "repaid", "liquidated", "defaulted", "force_liquidated")]
    locked = [l for l in loans if l["status"] in ("active", "awaiting_repayment")]
    outstanding = sum(l.get("creditSol", 0) for l in active)
    borrowed_all = sum(l.get("creditSol", 0) for l in funded)
    revenue = sum(l.get("interestSol", 0) for l in repaid)
    # TVL = real SOL value of collateral currently locked (loans where SOL was actually drawn)
    locked_value = sum((l.get("amount", 0) or 0) * (l.get("entryPriceSol", 0) or 0) for l in locked)
    wallets = {l.get("fromWallet") or l.get("recipient") for l in loans}
    wallets.discard(None)
    return {
        "liquiditySol": round(locked_value, 2),
        "creditOutstandingSol": round(outstanding, 2),
        "borrowedAllTimeSol": round(borrowed_all, 3),
        "revenueSol": round(revenue, 4),
        "activeLiens": len(active),
        "loansFunded": len(funded),
        "loansRepaid": len(repaid),
        "liquidations": len(liquidated),
        "tokensCount": len({l["tokenAddress"] for l in funded}),
        "users": len(wallets),
        "utilization": 0,
        "lpApr": 0,     # SOL supply not live yet
    }

@app.get("/api/tokens")
def tokens():
    """Curated watchlist, live only — tokens that don't resolve are omitted (no fake data)."""
    out = [fetch_token(s["address"]) for s in SEED_TOKENS]
    return {"tokens": [t for t in out if t]}

@app.get("/api/tokens/lookup")
def lookup(address: str = Query(...)):
    if len(address) < 32:
        raise HTTPException(400, "Invalid token address")
    t = get_token(address)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    return t

@app.get("/api/terms")
def terms():
    """Available loan terms. Shorter term = lower interest."""
    return {"terms": LOAN_TERMS, "default": DEFAULT_TERM}

@app.post("/api/loans/quote")
def quote(q: QuoteIn):
    t = get_token(q.tokenAddress)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    term = term_for(q.term)
    a = appraise(t, q.amount, q.boost, term["interest"])
    a["term"] = term
    return a

@app.post("/api/loans")
def open_loan(body: LoanIn):
    t = get_token(body.tokenAddress)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    term = term_for(body.term)
    a = appraise(t, body.amount, body.boost, term["interest"])
    if a["overCap"]:
        raise HTTPException(400, f"Exceeds Smart Cap — max credit {a['maxCreditSol']} SOL for this token")
    lock = new_lock_address()
    repay_addr = new_lock_address()
    now = time.time()
    loan = {
        "id": uuid.uuid4().hex[:12],
        "symbol": t["symbol"],
        "tokenAddress": t["address"],
        "amount": body.amount,
        "boost": body.boost,
        "recipient": body.recipient,   # may be None — credit returns to sender
        "fromWallet": None,
        "lockAddress": lock["address"],
        "repayAddress": repay_addr["address"],
        "entryPriceSol": t["priceSol"],
        "liqPriceSol": a["liqPriceSol"],
        "creditSol": a["creditSol"],
        "interestSol": a["interestSol"],
        "repaySol": a["repaySol"],
        "termKey": term["key"],
        "termLabel": term["label"],
        "termSeconds": term["seconds"],
        "termInterest": term["interest"],
        "dueAt": None,                # set at disbursement
        "status": "pending_deposit",  # -> active after deposit confirmed -> repaid|liquidated|defaulted
        "openedAt": now,
    }
    db = _load()
    db["loans"].append({**loan, "_lockSecret": lock.get("secret"),
                        "_repaySecret": repay_addr.get("secret")})
    _save(db)
    loan.pop("_lockSecret", None)
    return loan

@app.get("/api/loans")
def list_loans(recipient: str = Query(...)):
    db = _load()
    out = []
    for l in db["loans"]:
        if recipient not in (l.get("recipient"), l.get("fromWallet")):
            continue
        # health = current price / liquidation price (2.0 at entry, 1.0 at liq)
        liq = l.get("liqPriceSol") or 0
        cur = None
        try:
            tk = get_token(l["tokenAddress"])
            cur = tk["priceSol"] if tk else None
        except Exception:
            cur = None
        cur = cur if cur else l.get("entryPriceSol", liq * 2)
        health = round(cur / liq, 2) if liq else 2.0
        out.append({
            "id": l["id"], "symbol": l["symbol"], "amount": l["amount"],
            "creditSol": l["creditSol"], "repaySol": l["repaySol"],
            "liqPriceSol": liq, "health": health, "status": l["status"],
            "repayAddress": l.get("repayAddress"),
            "termLabel": l.get("termLabel"), "dueAt": l.get("dueAt"),
        })
    return {"loans": out}

@app.post("/api/loans/{loan_id}/confirm")
def confirm_deposit(loan_id: str, body: ConfirmIn = ConfirmIn()):
    """Borrower signals they've sent the tokens. Real flow watches the lock
    address on-chain, reads spot TOKEN/SOL price, and pays credit back to the
    sender. MVP records the intent and the sender wallet if provided."""
    db = _load()
    loan = next((l for l in db["loans"] if l["id"] == loan_id), None)
    if not loan:
        raise HTTPException(404, "Lien not found")
    if body and body.fromWallet:
        loan["fromWallet"] = body.fromWallet
        loan["recipient"] = loan.get("recipient") or body.fromWallet
    loan["status"] = "awaiting_deposit"
    loan["confirmedAt"] = time.time()
    _save(db)
    return {"id": loan_id, "status": loan["status"], "lockAddress": loan["lockAddress"]}

@app.get("/api/loans/{loan_id}")
def get_loan(loan_id: str):
    db = _load()
    l = next((x for x in db["loans"] if x["id"] == loan_id), None)
    if not l:
        raise HTTPException(404, "Loan not found")
    return {k: v for k, v in l.items() if not k.startswith("_")}

@app.get("/api/admin/health")
def admin_health(key: str = ""):
    """Show where the DB is written and whether that path is on a mounted volume."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, "forbidden")
    info = {"dataDir": str(DATA_DIR), "dbFile": str(DB_FILE),
            "dbExists": DB_FILE.exists(), "loanCount": len(_load().get("loans", []))}
    try:
        mounts = [m.split()[1] for m in Path("/proc/mounts").read_text().splitlines()
                  if len(m.split()) > 1 and m.split()[1] not in ("/", "/proc", "/sys", "/dev")]
        info["mountedPaths"] = mounts[:40]
        dd = str(DATA_DIR.resolve())
        info["dataDirOnVolume"] = any(dd == mp or dd.startswith(mp.rstrip("/") + "/") for mp in mounts)
    except Exception as e:
        info["mountError"] = str(e)
    try:
        t = DATA_DIR / ".writetest"; t.write_text("ok"); t.unlink(); info["writable"] = True
    except Exception as e:
        info["writable"] = False; info["writeError"] = str(e)[:80]
    return info

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

@app.get("/api/admin/loans")
def admin_loans(key: str = ""):
    """List recent loans (id, status, lock address) so the operator can find a stuck one."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, "forbidden")
    db = _load()
    rows = [{"id": l["id"], "status": l.get("status"), "symbol": l.get("symbol"),
             "amount": l.get("amount"), "lockAddress": l.get("lockAddress"),
             "fromWallet": l.get("fromWallet"), "watchNote": l.get("watchNote"),
             "disburseSig": l.get("disburseSig")} for l in db["loans"]]
    return {"loans": rows[-50:]}

@app.post("/api/admin/refund/{loan_id}")
def admin_refund(loan_id: str, to: str, key: str = ""):
    """Return locked collateral to wallet `to`. Operator-only. Use when a deposit can't disburse."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, "forbidden")
    db = _load()
    l = next((x for x in db["loans"] if x["id"] == loan_id), None)
    if not l:
        raise HTTPException(404, "loan not found")
    if engine.ENGINE.dry:
        raise HTTPException(400, "engine is in dry-run — arm it (set TREASURY_SECRET) first")
    if not l.get("_lockSecret"):
        raise HTTPException(400, "no lock key on this loan")
    # the lock address needs a little SOL to pay the transfer fee
    try:
        if engine.ENGINE.sol_balance(l["lockAddress"]) < 0.003:
            engine.ENGINE.send_sol(l["lockAddress"], engine.LOCK_GAS_SOL or 0.01)
            time.sleep(12)   # let the gas tx confirm
    except Exception as e:
        log.warning("refund gas top-up failed: %s", e)
    sig = engine.ENGINE.release_collateral(l["_lockSecret"], to, l["tokenAddress"], l["amount"])
    if str(sig).startswith("ERR"):
        raise HTTPException(500, f"refund failed: {sig} (retry in ~15s if gas was just sent)")
    l.update(status="cancelled", refundSig=sig, refundTo=to, closedAt=time.time(), watchNote=None)
    _save(db)
    return {"ok": True, "refundSig": sig, "solscan": f"https://solscan.io/tx/{sig}",
            "to": to, "amount": l["amount"], "symbol": l.get("symbol")}

@app.post("/api/loans/{loan_id}/repay")
def repay(loan_id: str):
    db = _load()
    loan = next((l for l in db["loans"] if l["id"] == loan_id), None)
    if not loan:
        raise HTTPException(404, "Lien not found")
    if loan["status"] in ("repaid", "liquidated", "defaulted"):
        raise HTTPException(400, f"Lien already {loan['status']}")
    # ---- live engine: collateral is released only after the repay SOL is received ----
    if engine.ENGINE_ENABLED and engine.ENGINE.ready and not engine.ENGINE.dry:
        if loan["status"] not in ("active", "awaiting_repayment"):
            raise HTTPException(400, "Loan is not active")
        loan["status"] = "awaiting_repayment"
        loan["repayRequestedAt"] = time.time()
        _save(db)
        return {"id": loan_id, "status": "awaiting_repayment",
                "repayAddress": loan.get("repayAddress"),
                "repaySol": loan.get("repaySol"), "symbol": loan["symbol"],
                "note": "Send exactly this much SOL to repayAddress from your wallet. "
                        "Your collateral is released automatically once it arrives."}
    # ---- dry-run / simulation: no real funds move, mark repaid immediately ----
    loan["status"] = "repaid"
    loan["closedAt"] = time.time()
    borrower = loan.get("fromWallet") or loan.get("recipient")
    if borrower:
        loan["releaseSig"] = engine.ENGINE.release_collateral(
            loan.get("_lockSecret"), borrower, loan["tokenAddress"], loan["amount"])
    _save(db)
    return {"id": loan_id, "status": "repaid", "released": loan["amount"],
            "symbol": loan["symbol"], "releaseSig": loan.get("releaseSig")}


# ============================ ENGINE WORKER ============================
def _disburse_ready_loans():
    """Pay SOL credit once the borrower's collateral lands at the lock address."""
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready):
        return
    if engine.LIQUIDATE_ALL or engine.WITHDRAW_ALL:   # emergency wind-down: no new payouts
        return
    db = _load(); changed = False
    outstanding = sum(l.get("creditSol", 0) for l in db["loans"] if l["status"] == "active")
    for l in db["loans"]:
        if l["status"] not in ("pending_deposit", "awaiting_deposit", "pending_approval") or l.get("disburseSig"):
            continue
        bal, _ = engine.ENGINE.token_balance(l["lockAddress"], l["tokenAddress"])
        if bal < l["amount"] * 0.99:            # collateral not arrived yet
            if l.get("watchNote") != "waiting_tokens": l["watchNote"] = "waiting_tokens"; changed = True
            continue
        wallet = l.get("fromWallet")
        if not wallet:                          # detect who funded the lock address
            wallet = engine.ENGINE.detect_sender(l["lockAddress"], l["tokenAddress"])
            if not wallet:
                log.warning("loan %s: deposit detected but sender unknown yet", l["id"])
                if l.get("watchNote") != "detecting_sender": l["watchNote"] = "detecting_sender"; changed = True
                continue
            l["fromWallet"] = wallet
            l["recipient"] = l.get("recipient") or wallet
            changed = True
        t = get_token(l["tokenAddress"])
        price = t["priceSol"] if t else l.get("entryPriceSol", 0)
        if not price:
            if l.get("watchNote") != "no_price": l["watchNote"] = "no_price"; changed = True
            continue
        vb = engine.ENGINE.vaultie_balance(wallet)
        rep = repaid_volume(db, wallet)
        ltv = effective_ltv(vb, rep)
        credit = round(l["amount"] * price * ltv, 6)

        # ---- safety rails ----
        if engine.MAX_LOAN_SOL and credit > engine.MAX_LOAN_SOL:
            if l.get("held") != "MAX_LOAN_SOL":
                l.update(status="held", held="MAX_LOAN_SOL", quotedCreditSol=credit); changed = True
            continue
        if engine.MAX_OUTSTANDING_SOL and (outstanding + credit) > engine.MAX_OUTSTANDING_SOL:
            if l.get("held") != "MAX_OUTSTANDING_SOL":
                l.update(status="held", held="MAX_OUTSTANDING_SOL", quotedCreditSol=credit); changed = True
            continue
        if engine.MAX_PER_WALLET_SOL:
            wallet_out = sum(x.get("creditSol", 0) for x in db["loans"]
                             if x["status"] in ("active", "awaiting_repayment")
                             and (x.get("fromWallet") or x.get("recipient")) == wallet)
            if wallet_out + credit > engine.MAX_PER_WALLET_SOL:
                # over the per-wallet limit — refund the deposit so the user isn't stranded
                if not engine.ENGINE.dry:
                    engine.ENGINE.send_sol(l["lockAddress"], engine.LOCK_GAS_SOL)
                    ref = engine.ENGINE.release_collateral(l.get("_lockSecret"), wallet,
                                                           l["tokenAddress"], l["amount"])
                else:
                    ref = "DRYRUN"
                l.update(status="refunded", held=None, refundSig=ref,
                         reason="per_wallet_cap", closedAt=time.time())
                changed = True
                continue
        if engine.MANUAL_APPROVAL and not l.get("approved"):
            if l["status"] != "pending_approval":
                l.update(status="pending_approval", quotedCreditSol=credit,
                         depositConfirmedBalance=bal); changed = True
            continue

        if not engine.ENGINE.dry:               # treasury guard
            tre = str(engine.ENGINE.treasury.pubkey())
            if engine.ENGINE.sol_balance(tre) < credit + engine.LOCK_GAS_SOL + 0.01:
                log.warning("treasury too low to fund loan %s (need %.4f)", l["id"], credit)
                if l.get("watchNote") != "treasury_low": l["watchNote"] = "treasury_low"; changed = True
                continue
        sig = engine.ENGINE.send_sol(wallet, credit)
        if sig.startswith("ERR"):
            continue
        # fund the lock address with a little SOL so it can later pay fees to release/liquidate
        if not engine.ENGINE.dry and engine.LOCK_GAS_SOL > 0:
            engine.ENGINE.send_sol(l["lockAddress"], engine.LOCK_GAS_SOL)
        term_i = l.get("termInterest", INTEREST)
        term_s = l.get("termSeconds", 0)
        disbursed = time.time()
        l.update(status="active", disburseSig=sig, creditSol=credit,
                 interestSol=round(credit * term_i, 6),
                 repaySol=round(credit * (1 + term_i), 6),
                 liqPriceSol=price * (1 - LIQ_DROP), entryPriceSol=price,
                 ltvApplied=ltv, boost=(ltv > LTV), disbursedAt=disbursed,
                 dueAt=(disbursed + term_s if term_s else None), held=None, watchNote=None)
        outstanding += credit
        changed = True
    if changed:
        _save(db)


def _check_liquidations():
    """Sell collateral for SOL when spot falls to the liquidation price."""
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready):
        return
    db = _load(); changed = False
    now = time.time()
    for l in db["loans"]:
        if l["status"] not in ("active", "awaiting_repayment") or l.get("liqSig"):
            continue
        # ---- term expired → borrower defaulted → collateral forfeited to protocol ----
        # (also applies while awaiting repayment, so the term can't be dodged)
        if l.get("dueAt") and now > l["dueAt"]:
            sig = engine.ENGINE.liquidate_swap(l.get("_lockSecret"), l["tokenAddress"], l["amount"])
            if sig.startswith("ERR"):
                continue
            l.update(status="defaulted", liqSig=sig, closedAt=now,
                     defaultReason="term_expired", sweepPending=True)
            changed = True
            continue
        if l["status"] != "active":
            continue
        t = get_token(l["tokenAddress"])
        if not t:
            continue
        if t["priceSol"] <= (l.get("liqPriceSol") or 0):
            sig = engine.ENGINE.liquidate_swap(l.get("_lockSecret"), l["tokenAddress"], l["amount"])
            if sig.startswith("ERR"):
                continue
            l.update(status="liquidated", liqSig=sig, closedAt=now,
                     liqPriceObserved=t["priceSol"])
            changed = True
    if changed:
        _save(db)


def _check_repayments():
    """Release collateral once the borrower's repay SOL lands on the loan's repay address."""
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready) or engine.ENGINE.dry:
        return
    db = _load(); changed = False
    tre = str(engine.ENGINE.treasury.pubkey())
    for l in db["loans"]:
        if l["status"] != "awaiting_repayment" or l.get("releaseSig"):
            continue
        addr = l.get("repayAddress"); need = l.get("repaySol") or 0
        if not addr or need <= 0:
            continue
        bal = engine.ENGINE.sol_balance(addr)
        if bal < need * 0.999:   # not fully paid yet
            note = f"received {bal:.4f} / need {need:.4f} SOL"
            if l.get("repayNote") != note: l["repayNote"] = note; changed = True
            continue
        borrower = l.get("fromWallet") or l.get("recipient")
        if not borrower:
            continue
        # repayment received — ensure the lock address has gas to pay the release fee
        if engine.ENGINE.sol_balance(l["lockAddress"]) < 0.003:
            engine.ENGINE.send_sol(l["lockAddress"], engine.LOCK_GAS_SOL or 0.01)
            if l.get("repayNote") != "funding_gas": l["repayNote"] = "funding_gas"; changed = True
            continue   # release on the next pass once gas confirms
        rel = engine.ENGINE.release_collateral(l.get("_lockSecret"), borrower, l["tokenAddress"], l["amount"])
        if rel.startswith("ERR"):
            if l.get("repayNote") != "release_retry": l["repayNote"] = "release_retry:" + rel[:30]; changed = True
            continue
        swp = engine.ENGINE.sweep_sol(l.get("_repaySecret"), tre)   # move repayment into treasury
        l.update(status="repaid", releaseSig=rel, sweepSig=swp, repayNote=None,
                 closedAt=time.time(), repaidAt=time.time())
        changed = True
    if changed:
        _save(db)


def _sweep_proceeds():
    """After a forfeited loan's collateral is sold, move the SOL proceeds into the treasury — automatically."""
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready) or engine.ENGINE.dry:
        return
    db = _load(); changed = False
    tre = str(engine.ENGINE.treasury.pubkey())
    for l in db["loans"]:
        if not l.get("sweepPending") or l.get("sweepSig"):
            continue
        if engine.ENGINE.sol_balance(l["lockAddress"]) < 0.002:   # swap proceeds not settled yet
            continue
        sig = engine.ENGINE.sweep_sol(l.get("_lockSecret"), tre)
        if sig.startswith("ERR"):
            continue
        l.update(sweepSig=sig, sweepPending=False, sweptToTreasury=True)
        changed = True
    if changed:
        _save(db)


def _liquidate_all():
    """EMERGENCY: when LIQUIDATE_ALL=1, sell every open position's collateral; proceeds sweep to treasury."""
    if not engine.LIQUIDATE_ALL:
        return
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready) or engine.ENGINE.dry:
        return
    db = _load(); changed = False
    now = time.time()
    for l in db["loans"]:
        if l["status"] not in ("active", "awaiting_repayment") or l.get("liqSig"):
            continue
        sig = engine.ENGINE.liquidate_swap(l.get("_lockSecret"), l["tokenAddress"], l["amount"])
        if sig.startswith("ERR"):
            continue
        l.update(status="force_liquidated", liqSig=sig, closedAt=now,
                 defaultReason="liquidate_all", sweepPending=True)
        changed = True
    if changed:
        _save(db)


def _withdraw_all():
    """EMERGENCY: when WITHDRAW_ALL=1, move every open position's collateral TOKENS to the treasury wallet."""
    if not engine.WITHDRAW_ALL:
        return
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready) or engine.ENGINE.dry:
        return
    db = _load(); changed = False
    now = time.time()
    tre = str(engine.ENGINE.treasury.pubkey())
    for l in db["loans"]:
        if l["status"] not in ("active", "awaiting_repayment") or l.get("withdrawSig"):
            continue
        # the lock address needs a little SOL to pay the transfer fee
        if engine.ENGINE.sol_balance(l["lockAddress"]) < 0.003:
            engine.ENGINE.send_sol(l["lockAddress"], engine.LOCK_GAS_SOL or 0.01)
            continue   # let gas confirm; move it next pass
        sig = engine.ENGINE.release_collateral(l.get("_lockSecret"), tre, l["tokenAddress"], l["amount"])
        if str(sig).startswith("ERR"):
            continue
        l.update(status="withdrawn", withdrawSig=sig, withdrawnTo=tre, closedAt=now, watchNote=None)
        changed = True
    if changed:
        _save(db)


def _engine_loop():
    poll = int(os.getenv("ENGINE_POLL", "20"))
    log.info("engine loop started (poll=%ss, dry=%s)", poll, engine.ENGINE.dry)
    while True:
        try:
            _disburse_ready_loans()
            _check_repayments()
            _check_liquidations()
            _liquidate_all()
            _withdraw_all()
            _sweep_proceeds()
        except Exception as e:
            log.warning("engine loop error: %s", e)
        time.sleep(poll)


@app.on_event("startup")
def _start_engine():
    if not engine.ENGINE_ENABLED:
        log.info("engine disabled (ENGINE_ENABLED=0)")
        return
    import threading
    threading.Thread(target=_engine_loop, daemon=True).start()


@app.get("/api/engine/status")
def engine_status():
    return engine.status()


@app.get("/api/account/{wallet}")
def account(wallet: str):
    """A wallet's borrowing power: $VAULTIE tier + repayment reputation → effective LTV."""
    db = _load()
    vb = engine.ENGINE.vaultie_balance(wallet) if engine.ENGINE.ready else 0.0
    rep_vol = repaid_volume(db, wallet)
    return {
        "wallet": wallet,
        "vaultieBalance": vb,
        "repaidVolumeSol": round(rep_vol, 3),
        "baseLtv": LTV,
        "stakeLtv": stake_ltv(vb),
        "reputationBonus": round(reputation_bonus(rep_vol), 4),
        "effectiveLtv": effective_ltv(vb, rep_vol),
        "ltvMax": LTV_MAX,
        "tiers": [{"min": t, "ltv": v} for t, v in LTV_TIERS],
    }


@app.post("/api/loans/{loan_id}/approve")
def approve_loan(loan_id: str):
    """Operator approval for MANUAL_APPROVAL mode: release a held/pending payout."""
    db = _load()
    loan = next((l for l in db["loans"] if l["id"] == loan_id), None)
    if not loan:
        raise HTTPException(404, "loan not found")
    loan["approved"] = True
    if loan["status"] in ("pending_approval", "held"):
        loan["status"] = "awaiting_deposit"      # next engine pass will pay it
        loan["held"] = None
    _save(db)
    return {"id": loan_id, "approved": True, "status": loan["status"]}

@app.post("/api/staking/sol")
def stake_sol(body: StakeIn):
    if body.amount <= 0 or len(body.recipient) < 32:
        raise HTTPException(400, "Invalid input")
    lock = new_lock_address()
    db = _load()
    db["lp"].append({"recipient": body.recipient, "amount": body.amount,
                     "depositAddress": lock["address"], "_secret": lock.get("secret"),
                     "at": time.time()})
    _save(db)
    return {"depositAddress": lock["address"], "amount": body.amount}

@app.post("/api/staking/token")
def stake_token(body: StakeIn):
    if body.amount <= 0 or len(body.recipient) < 32:
        raise HTTPException(400, "Invalid input")
    db = _load()
    db["stakes"][body.recipient] = db["stakes"].get(body.recipient, 0) + body.amount
    _save(db)
    return {"recipient": body.recipient, "staked": db["stakes"][body.recipient],
            "boostActive": db["stakes"][body.recipient] >= BOOST_MIN, "boostMin": BOOST_MIN}

@app.get("/")
def root():
    return {"service": "vaultie", "ok": True, "custodial": True,
            "note": "MVP — custodial, off-chain. Not audited. High risk."}
