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
LIQ_DROP     = float(os.getenv("LIQ_DROP", "0.50"))
CAP          = float(os.getenv("SMART_CAP", "0.10"))
BOOST_MIN    = int(os.getenv("BOOST_MIN", "10000000"))
SOL_USD      = float(os.getenv("SOL_USD", "165"))          # fallback only; live pairs derive SOL price from Dexscreener
DATA_DIR     = Path(os.getenv("DATA_DIR", "./data"))
ORIGINS      = os.getenv("CORS_ORIGINS", "*").split(",")

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "vaultie.json"

# ---- seed token registry (replace addresses/liquidity with a live source) ----
SEED_TOKENS = [
    {"symbol": "WIF", "name": "dogwifhat", "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "liquiditySol": 9200, "priceUsd": 0.0204, "change24h": 6.1},
    {"symbol": "POPCAT", "name": "Popcat", "address": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "liquiditySol": 6400, "priceUsd": 0.51, "change24h": -3.4},
    {"symbol": "PNUT", "name": "Peanut", "address": "2qEHjDLDLbuBgRYvsxhc5NEU7g3p6vNJ7NXG7vN9zcjm", "liquiditySol": 3100, "priceUsd": 0.142, "change24h": 11.7},
    {"symbol": "GIGA", "name": "Gigachad", "address": "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9", "liquiditySol": 2700, "priceUsd": 0.031, "change24h": 2.8},
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


def _seed_enrich(t: dict) -> dict:
    """Give a seed token the same shape as a live one (uses fallback SOL_USD)."""
    price_sol = t["priceUsd"] / SOL_USD
    return {**t, "priceSol": price_sol, "liquidityUsd": t["liquiditySol"] * SOL_USD,
            "solUsd": SOL_USD, "live": False}


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
    """Live first, seed fallback by address, else None."""
    live = fetch_token(address)
    if live:
        return live
    seed = next((t for t in SEED_TOKENS if t["address"] == address), None)
    return _seed_enrich(seed) if seed else None


def appraise(token, amount, boost):
    """Appraise against the live spot price. Works in SOL directly via priceSol."""
    ltv = LTV_BOOST if boost else LTV
    value_usd = amount * token["priceUsd"]
    value_sol = amount * token["priceSol"]
    credit_sol = value_sol * ltv
    interest_sol = credit_sol * INTEREST
    liq_price_sol = token["priceSol"] * (1 - LIQ_DROP)     # -50% from entry
    cap_credit_sol = token["liquiditySol"] * CAP * ltv      # Smart Cap drawable
    return {
        "appraisedUsd": round(value_usd, 2),
        "appraisedSol": round(value_sol, 4),
        "ltv": ltv,
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

class LoanIn(QuoteIn):
    symbol: Optional[str] = None
    recipient: Optional[str] = None   # not required: credit returns to the sending wallet

class ConfirmIn(BaseModel):
    fromWallet: Optional[str] = None  # wallet the deposit was sent from (optional hint)

class StakeIn(BaseModel):
    amount: float
    recipient: str


# ---------- routes ----------
@app.get("/api/protocol/stats")
def stats():
    db = _load()
    active = [l for l in db["loans"] if l["status"] == "active"]
    outstanding = sum(l["repaySol"] for l in active)
    liquidity = sum(x["amount"] for x in db["lp"]) or 14820.0
    util = min(outstanding / liquidity, 0.95) if liquidity else 0
    return {
        "liquiditySol": round(liquidity, 2),
        "creditOutstandingSol": round(outstanding, 2),
        "activeLiens": len(active),
        "lpApr": round(0.08 + util * 0.40, 4),  # dynamic: base + utilization
    }

@app.get("/api/tokens")
def tokens():
    """Curated watchlist, enriched live where possible."""
    out = []
    for s in SEED_TOKENS:
        out.append(fetch_token(s["address"]) or _seed_enrich(s))
    return {"tokens": out}

@app.get("/api/tokens/lookup")
def lookup(address: str = Query(...)):
    if len(address) < 32:
        raise HTTPException(400, "Invalid token address")
    t = get_token(address)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    return t

@app.post("/api/loans/quote")
def quote(q: QuoteIn):
    t = get_token(q.tokenAddress)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    return appraise(t, q.amount, q.boost)

@app.post("/api/loans")
def open_loan(body: LoanIn):
    t = get_token(body.tokenAddress)
    if not t:
        raise HTTPException(404, "No Solana market found for this token")
    a = appraise(t, body.amount, body.boost)
    if a["overCap"]:
        raise HTTPException(400, f"Exceeds Smart Cap — max credit {a['maxCreditSol']} SOL for this token")
    lock = new_lock_address()
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
        "entryPriceSol": t["priceSol"],
        "liqPriceSol": a["liqPriceSol"],
        "creditSol": a["creditSol"],
        "interestSol": a["interestSol"],
        "repaySol": a["repaySol"],
        "status": "pending_deposit",  # -> active after deposit confirmed -> repaid|liquidated
        "openedAt": now,
    }
    db = _load()
    db["loans"].append({**loan, "_lockSecret": lock.get("secret")})
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

@app.post("/api/loans/{loan_id}/repay")
def repay(loan_id: str):
    db = _load()
    loan = next((l for l in db["loans"] if l["id"] == loan_id), None)
    if not loan:
        raise HTTPException(404, "Lien not found")
    if loan["status"] in ("repaid", "liquidated"):
        raise HTTPException(400, f"Lien already {loan['status']}")
    # MVP: mark repaid. Real flow verifies inbound SOL = repaySol, then releases collateral.
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
    db = _load(); changed = False
    for l in db["loans"]:
        if l["status"] != "awaiting_deposit" or l.get("disburseSig"):
            continue
        wallet = l.get("fromWallet")
        if not wallet:
            continue
        bal, _ = engine.ENGINE.token_balance(l["lockAddress"], l["tokenAddress"])
        if bal < l["amount"] * 0.99:            # collateral not arrived yet
            continue
        t = get_token(l["tokenAddress"])
        price = t["priceSol"] if t else l.get("entryPriceSol", 0)
        if not price:
            continue
        boost = engine.ENGINE.vaultie_balance(wallet) >= BOOST_MIN
        ltv = LTV_BOOST if boost else LTV
        credit = round(l["amount"] * price * ltv, 6)
        if not engine.ENGINE.dry:               # treasury guard
            tre = str(engine.ENGINE.treasury.pubkey())
            if engine.ENGINE.sol_balance(tre) < credit + 0.01:
                log.warning("treasury too low to fund loan %s (need %.4f)", l["id"], credit)
                continue
        sig = engine.ENGINE.send_sol(wallet, credit)
        if sig.startswith("ERR"):
            continue
        l.update(status="active", disburseSig=sig, creditSol=credit,
                 interestSol=round(credit * INTEREST, 6),
                 repaySol=round(credit * (1 + INTEREST), 6),
                 liqPriceSol=price * (1 - LIQ_DROP), entryPriceSol=price,
                 boost=boost, disbursedAt=time.time())
        changed = True
    if changed:
        _save(db)


def _check_liquidations():
    """Sell collateral for SOL when spot falls to the liquidation price."""
    if not (engine.ENGINE_ENABLED and engine.ENGINE.ready):
        return
    db = _load(); changed = False
    for l in db["loans"]:
        if l["status"] != "active" or l.get("liqSig"):
            continue
        t = get_token(l["tokenAddress"])
        if not t:
            continue
        if t["priceSol"] <= (l.get("liqPriceSol") or 0):
            sig = engine.ENGINE.liquidate_swap(l.get("_lockSecret"), l["tokenAddress"], l["amount"])
            if sig.startswith("ERR"):
                continue
            l.update(status="liquidated", liqSig=sig, closedAt=time.time(),
                     liqPriceObserved=t["priceSol"])
            changed = True
    if changed:
        _save(db)


def _engine_loop():
    poll = int(os.getenv("ENGINE_POLL", "20"))
    log.info("engine loop started (poll=%ss, dry=%s)", poll, engine.ENGINE.dry)
    while True:
        try:
            _disburse_ready_loans()
            _check_liquidations()
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
