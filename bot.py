import asyncio
import aiohttp
import logging
import os
import time
import json
import base58
import base64
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN      = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT       = os.getenv("TG_CHAT",  "5667140911")
HELIUS_KEY    = os.getenv("HELIUS_KEY", "85dee6a1-d8e2-421e-8a26-33645c4a943f")
WALLET_KEY    = os.getenv("WALLET_PRIVATE_KEY", "")
SCAN_EVERY    = int(os.getenv("SCAN_EVERY", "30"))

# Trading config
BUY_AMOUNT_SOL   = float(os.getenv("BUY_AMOUNT_SOL", "0.1"))
MAX_TRADES       = int(os.getenv("MAX_TRADES", "3"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "0.5"))
TRAILING_STOP    = float(os.getenv("TRAILING_STOP", "0.20"))

# Delutgångar
TP1_PCT = 1.0
TP2_PCT = 3.0
TP3_PCT = 7.0

# Market filters — justerade
MIN_LIQ            = 10_000
MIN_VOL_1H         = 2_000
MIN_BUYS_1H        = 30
BUY_SELL_RATIO     = 2.0
MAX_AGE_MIN        = 90
MIN_AGE_MIN        = 3
MIN_PCT5M          = 1.0
MIN_MCAP           = 20_000
MAX_MCAP           = 500_000
MIN_LIQ_MCAP_RATIO = 0.10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pumpscan")

# State
notified      = set()
last_check    = {}
active_trades = {}
daily_loss    = 0.0
RECHECK_AFTER = 600

def fmt(n):
    if not n: return "N/A"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    if n >= 1e3: return f"${n/1e3:.1f}K"
    return f"${n:.0f}"

def age_min(ts):
    if not ts: return 9999
    t = ts if isinstance(ts, (int, float)) else 0
    return (time.time()*1000 - t) / 60_000

def get_keypair() -> Keypair:
    raw = WALLET_KEY.strip()
    if raw.startswith("["):
        key_bytes = bytes(json.loads(raw))
    else:
        key_bytes = base58.b58decode(raw)
    if len(key_bytes) == 32:
        return Keypair.from_seed(key_bytes)
    else:
        return Keypair.from_bytes(key_bytes)

def get_public_key_str() -> str:
    try:
        return str(get_keypair().pubkey())
    except Exception as e:
        log.error(f"Public key fel: {e}")
        return ""

def sign_and_encode(tx_base64: str) -> str:
    keypair = get_keypair()
    raw_tx = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()

async def send_tg(session, msg):
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"TG fel {r.status}")
            else:
                log.info("📱 Telegram skickad!")
    except Exception as e:
        log.warning(f"TG exception: {e}")

async def get_sol_price(session):
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            d = await r.json(content_type=None)
            return d["solana"]["usd"]
    except:
        return 180.0

async def send_transaction(session, tx_base64: str):
    try:
        signed_tx = sign_and_encode(tx_base64)
    except Exception as e:
        log.error(f"Signering fel: {e}")
        return None
    try:
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [signed_tx, {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3
                }]
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            result = await r.json(content_type=None)
            if result.get("error"):
                log.warning(f"Send TX fel: {result['error']}")
                return None
            return result.get("result")
    except Exception as e:
        log.error(f"Send TX exception: {e}")
        return None

async def buy_token(session, token_addr, token_symbol):
    global daily_loss

    if not WALLET_KEY:
        log.warning("Ingen wallet key!")
        return None
    if len(active_trades) >= MAX_TRADES:
        log.info(f"Max trades ({MAX_TRADES}) nådda")
        return None
    if daily_loss >= DAILY_LOSS_LIMIT:
        await send_tg(session,
            f"🛑 *Daglig förlustgräns nådd!*\n"
            f"Förlorat {daily_loss:.3f} SOL idag.\n"
            f"Boten pausar tills imorgon. 🔒"
        )
        return None

    try:
        sol_price = await get_sol_price(session)
        buy_lamports = int(BUY_AMOUNT_SOL * 1e9)
        WSOL = "So11111111111111111111111111111111111111112"

        log.info(f"💰 Köper {token_symbol} för {BUY_AMOUNT_SOL} SOL...")

        async with session.get(
            f"https://lite-api.jup.ag/swap/v1/quote"
            f"?inputMint={WSOL}&outputMint={token_addr}"
            f"&amount={buy_lamports}&slippageBps=2000",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"Jupiter quote fel: {r.status}")
                return None
            quote = await r.json(content_type=None)

        if not quote or "outAmount" not in quote:
            return None

        async with session.post(
            "https://lite-api.jup.ag/swap/v1/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": get_public_key_str(),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": 50000,
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                log.warning(f"Jupiter swap fel: {r.status}")
                return None
            swap_data = await r.json(content_type=None)

        if not swap_data.get("swapTransaction"):
            return None

        tx_sig = await send_transaction(session, swap_data["swapTransaction"])
        if not tx_sig:
            return None

        log.info(f"  ✅ Köpt! TX: {tx_sig[:20]}...")

        buy_price = await get_token_price(session, token_addr)
        active_trades[token_addr] = {
            "buy_price": buy_price,
            "peak_price": buy_price,
            "amount_sol": BUY_AMOUNT_SOL,
            "buy_time": time.time(),
            "tp1_done": False,
            "tp2_done": False,
            "tp3_done": False,
            "symbol": token_symbol,
            "tx": tx_sig,
        }
        return tx_sig

    except Exception as e:
        log.error(f"Köp-fel: {e}")
        return None

async def sell_token(session, token_addr, reason="manual", pct=100):
    if token_addr not in active_trades:
        return None

    trade = active_trades[token_addr]

    try:
        WSOL = "So11111111111111111111111111111111111111112"
        balance = await get_token_balance(session, token_addr)
        if not balance or balance == 0:
            active_trades.pop(token_addr, None)
            return None

        sell_amount = int(balance * (pct / 100))
        if sell_amount == 0:
            return None

        log.info(f"💸 Säljer {pct}% av {trade['symbol']} — {reason}")

        async with session.get(
            f"https://lite-api.jup.ag/swap/v1/quote"
            f"?inputMint={token_addr}&outputMint={WSOL}"
            f"&amount={sell_amount}&slippageBps=2500",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            quote = await r.json(content_type=None)

        if not quote: return None

        async with session.post(
            "https://lite-api.jup.ag/swap/v1/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": get_public_key_str(),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": 50000,
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200: return None
            swap_data = await r.json(content_type=None)

        if not swap_data.get("swapTransaction"): return None

        tx_sig = await send_transaction(session, swap_data["swapTransaction"])

        if pct == 100:
            active_trades.pop(token_addr, None)

        return tx_sig

    except Exception as e:
        log.error(f"Sälj-fel: {e}")
        return None

async def get_token_price(session, token_addr):
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)
            pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs: return None
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
            return float(best.get("priceNative") or 0)
    except:
        return None

async def get_token_balance(session, token_addr):
    try:
        pub_key = get_public_key_str()
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [pub_key, {"mint": token_addr}, {"encoding": "jsonParsed"}]
            },
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            result = await r.json(content_type=None)
            accounts = result.get("result", {}).get("value", [])
            if not accounts: return 0
            return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except:
        return 0

async def monitor_trades(session):
    while True:
        try:
            for addr in list(active_trades.keys()):
                trade = active_trades.get(addr)
                if not trade: continue

                current_price = await get_token_price(session, addr)
                if not current_price or not trade["buy_price"]:
                    continue

                if current_price > (trade["peak_price"] or 0):
                    trade["peak_price"] = current_price

                pct_change = (current_price - trade["buy_price"]) / trade["buy_price"]
                pct_from_peak = (current_price - trade["peak_price"]) / trade["peak_price"] if trade["peak_price"] else 0
                symbol = trade["symbol"]

                log.info(f"📊 {symbol}: {pct_change*100:+.1f}% (peak: {((trade['peak_price']/trade['buy_price'])-1)*100:+.1f}%)")

                if pct_from_peak <= -TRAILING_STOP and trade["tp1_done"]:
                    log.info(f"📉 Trailing stop {symbol}")
                    await sell_token(session, addr, reason="trailing_stop", pct=100)
                    profit_loss = trade["amount_sol"] * pct_change
                    global daily_loss
                    if profit_loss < 0:
                        daily_loss += abs(profit_loss)
                    await send_tg(session,
                        f"📉 *TRAILING STOP — {symbol}*\n"
                        f"Sålde vid {pct_change*100:+.1f}% från köp\n"
                        f"Föll {pct_from_peak*100:.1f}% från toppen\n"
                        f"{'Vinst' if profit_loss > 0 else 'Förlust'}: {abs(profit_loss):.3f} SOL\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                elif pct_change <= -0.20 and not trade["tp1_done"]:
                    log.info(f"🛑 Stop loss {symbol}")
                    await sell_token(session, addr, reason="stop_loss", pct=100)
                    loss_sol = trade["amount_sol"] * 0.20
                    daily_loss += loss_sol
                    await send_tg(session,
                        f"🛑 *STOP LOSS — {symbol}*\n"
                        f"Sålde vid {pct_change*100:.1f}%\n"
                        f"Förlorade ca {loss_sol:.3f} SOL\n"
                        f"Daglig förlust: {daily_loss:.3f} SOL\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                elif pct_change >= TP1_PCT and not trade["tp1_done"]:
                    await sell_token(session, addr, reason="tp1", pct=50)
                    trade["tp1_done"] = True
                    profit = trade["amount_sol"] * 0.5 * TP1_PCT
                    await send_tg(session,
                        f"🎯 *TP1 +100% — {symbol}*\n"
                        f"Sålde 50% — vinst: +{profit:.3f} SOL 💰\n"
                        f"Håller 50% med trailing stop\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                elif pct_change >= TP2_PCT and trade["tp1_done"] and not trade["tp2_done"]:
                    await sell_token(session, addr, reason="tp2", pct=50)
                    trade["tp2_done"] = True
                    profit = trade["amount_sol"] * 0.25 * TP2_PCT
                    await send_tg(session,
                        f"🚀 *TP2 +300% — {symbol}*\n"
                        f"Sålde 25% — vinst: +{profit:.3f} SOL 💰🔥\n"
                        f"Håller 25% som moonbag\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                elif pct_change >= TP3_PCT and trade["tp2_done"] and not trade["tp3_done"]:
                    await sell_token(session, addr, reason="tp3", pct=50)
                    trade["tp3_done"] = True
                    profit = trade["amount_sol"] * 0.25 * TP3_PCT
                    await send_tg(session,
                        f"🌙 *TP3 +700% — {symbol}*\n"
                        f"Sålde 25% — vinst: +{profit:.3f} SOL 🌙💰\n"
                        f"Håller moonbag\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                await asyncio.sleep(2)

        except Exception as e:
            log.error(f"Monitor fel: {e}")

        await asyncio.sleep(30)

async def full_rugcheck(session, addr):
    try:
        async with session.get(
            f"https://api.rugcheck.xyz/v1/tokens/{addr}/report",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)

        freeze_auth = d.get("freezeAuthority") or d.get("freeze_authority")
        has_freeze = freeze_auth not in [None, "", "null"]
        mint_auth = d.get("mintAuthority") or d.get("mint_authority")
        has_mint = mint_auth not in [None, "", "null"]
        mutable = d.get("mutableMetadata") or False

        top_holders = d.get("topHolders") or []
        max_holder_pct = 0
        top10_pct = 0
        non_lp = [h for h in top_holders if not h.get("isLpHolder")]
        if non_lp:
            max_holder_pct = max(h.get("pct", 0) for h in non_lp)
            top10_pct = sum(h.get("pct", 0) for h in non_lp[:10])

        markets = d.get("markets") or []
        lp_pct = 0
        if markets:
            lp_pct = float(markets[0].get("lpLockedPct") or 0)

        score = 0
        if isinstance(d.get("score"), (int, float)):
            score = min(100, max(0, round(d["score"])))

        risks = d.get("risks") or []
        risk_names = [r.get("name", "").lower() for r in risks]
        is_honeypot = any("honeypot" in r for r in risk_names)
        has_freeze_risk = any("freeze" in r for r in risk_names)
        has_mint_risk = any("mint" in r for r in risk_names)
        high_risks = [r for r in risks if r.get("level", "").lower() == "danger"]

        return {
            "score": score,
            "freeze_authority": has_freeze or has_freeze_risk,
            "mint_authority": has_mint or has_mint_risk,
            "mutable_metadata": mutable,
            "max_holder_pct": max_holder_pct,
            "top10_pct": top10_pct,
            "lp_pct": lp_pct,
            "is_honeypot": is_honeypot,
            "high_risks": high_risks,
        }
    except Exception as e:
        log.warning(f"RugCheck fel: {e}")
        return None

def is_safe(rc, liq, mcap):
    if rc["freeze_authority"]: return False
    if rc["mint_authority"]: return False
    if rc["is_honeypot"]: return False
    if rc["mutable_metadata"]: return False
    if rc["max_holder_pct"] > 10: return False
    if rc["top10_pct"] > 50: return False
    if len(rc["high_risks"]) > 0: return False
    if rc["score"] < 40: return False
    if liq < MIN_LIQ: return False
    if mcap > 0 and liq / mcap < MIN_LIQ_MCAP_RATIO: return False
    return True

async def analyze_and_buy(session, pair):
    addr   = (pair.get("baseToken") or {}).get("address", "")
    if not addr or addr in notified: return

    name   = (pair.get("baseToken") or {}).get("name", "?")
    ticker = (pair.get("baseToken") or {}).get("symbol", "?")
    liq    = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol1h  = (pair.get("volume") or {}).get("h1", 0) or 0
    buys   = (pair.get("txns") or {}).get("h1", {}).get("buys", 0) or 0
    sells  = (pair.get("txns") or {}).get("h1", {}).get("sells", 1) or 1
    mcap   = pair.get("fdv") or pair.get("marketCap") or 0
    age    = age_min(pair.get("pairCreatedAt"))
    pct5m  = (pair.get("priceChange") or {}).get("m5", 0) or 0
    pct1h  = (pair.get("priceChange") or {}).get("h1", 0) or 0
    ratio  = buys / max(sells, 1)
    dex    = pair.get("dexId", "")

    if "raydium" not in dex.lower(): return
    if age < MIN_AGE_MIN or age > MAX_AGE_MIN: return
    if liq < MIN_LIQ or vol1h < MIN_VOL_1H: return
    if buys < MIN_BUYS_1H: return
    if ratio < BUY_SELL_RATIO: return
    if pct5m < MIN_PCT5M: return
    if mcap < MIN_MCAP or mcap > MAX_MCAP: return

    log.info(f"🔍 {name} age={age:.0f}min liq={fmt(liq)} vol={fmt(vol1h)} buys={buys} mcap={fmt(mcap)} +{pct5m:.1f}%")

    rc = await full_rugcheck(session, addr)
    await asyncio.sleep(0.5)

    if not rc:
        log.info(f"  → Ingen RugCheck — skippar")
        return

    if not is_safe(rc, liq, mcap):
        log.info(f"  → NOGO — dev:{rc['max_holder_pct']:.0f}% top10:{rc['top10_pct']:.0f}% score:{rc['score']}")
        return

    notified.add(addr)
    log.info(f"  → ✅ GO! Köper {name}...")

    tx_sig = await buy_token(session, addr, ticker)

    if tx_sig:
        sol_price = await get_sol_price(session)
        liq_mcap = f"{(liq/mcap*100):.0f}%" if mcap > 0 else "N/A"
        await send_tg(session,
            f"🔥 *KÖPT: {name}* (${ticker})\n"
            f"⏱ {age:.0f} min | Raydium\n\n"
            f"💰 {BUY_AMOUNT_SOL} SOL (${BUY_AMOUNT_SOL*sol_price:.0f})\n"
            f"💧 Liq: {fmt(liq)} ({liq_mcap} av MCap)\n"
            f"📊 MCap: {fmt(mcap)}\n"
            f"📈 Vol 1h: {fmt(vol1h)} | Köp: {buys}\n"
            f"🔄 Ratio: {ratio:.1f}x | +{pct5m:.1f}% (5min)\n"
            f"🛡 Score: {rc['score']}/100\n"
            f"👛 Dev: {rc['max_holder_pct']:.0f}% | Top10: {rc['top10_pct']:.0f}%\n\n"
            f"🎯 TP1: +100% | TP2: +300% | TP3: +700%\n"
            f"📉 Trailing stop: -20% från topp\n\n"
            f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n\n"
            f"⚡ _PumpScan Bot v5_"
        )
    else:
        await send_tg(session,
            f"⚠️ *GO men köp misslyckades: {name}*\n"
            f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n\n"
            f"⚡ _PumpScan Bot v5_"
        )

async def dex_scan_loop(session):
    while True:
        try:
            log.info("🔍 Scannar Raydium nylistningar...")
            all_pairs = []

            urls = [
                "https://api.dexscreener.com/latest/dex/search?q=raydium",
                "https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-profiles/latest/v1",
            ]

            for url in urls:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200: continue
                        d = await r.json(content_type=None)
                        if isinstance(d, list):
                            for item in d:
                                chain = item.get("chainId") or item.get("chain", "")
                                if chain == "solana":
                                    addr = item.get("tokenAddress") or item.get("address", "")
                                    if addr and addr not in notified and addr not in last_check:
                                        try:
                                            async with session.get(
                                                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                                                timeout=aiohttp.ClientTimeout(total=8)
                                            ) as r2:
                                                if r2.status == 200:
                                                    d2 = await r2.json(content_type=None)
                                                    token_pairs = [
                                                        p for p in (d2.get("pairs") or [])
                                                        if p.get("chainId") == "solana"
                                                        and "raydium" in p.get("dexId", "").lower()
                                                    ]
                                                    if token_pairs:
                                                        all_pairs.append(max(token_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0))
                                        except: pass
                        else:
                            for p in (d.get("pairs") or []):
                                if (p.get("chainId") == "solana" and
                                        "raydium" in p.get("dexId", "").lower()):
                                    all_pairs.append(p)
                except Exception as e:
                    log.warning(f"Scan error: {e}")
                await asyncio.sleep(0.3)

            seen = set()
            now = time.time()
            expired = [a for a, t in list(last_check.items()) if now - t > 1800 and a not in notified]
            for a in expired:
                del last_check[a]

            fresh = []
            for p in all_pairs:
                addr = (p.get("baseToken") or {}).get("address", "")
                if not addr or addr in seen or addr in notified: continue
                if now - last_check.get(addr, 0) < RECHECK_AFTER: continue
                seen.add(addr)
                age = age_min(p.get("pairCreatedAt"))
                liq = (p.get("liquidity") or {}).get("usd", 0) or 0
                if MIN_AGE_MIN <= age <= MAX_AGE_MIN and liq >= MIN_LIQ:
                    fresh.append(p)
                    last_check[addr] = now

            log.info(f"📦 {len(fresh)} nya Raydium coins")
            fresh.sort(key=lambda p: (p.get("volume") or {}).get("h1", 0) or 0, reverse=True)
            for p in fresh[:8]:
                await analyze_and_buy(session, p)
                await asyncio.sleep(1)

        except Exception as e:
            log.error(f"Scan fel: {e}")
        await asyncio.sleep(SCAN_EVERY)

async def main():
    log.info("🚀 PumpScan Bot v5 — Smart Edition startar...")
    conn = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        sol_price = await get_sol_price(session)
        await send_tg(session,
            f"🤖 *PumpScan Bot v5 — Smart Edition*\n\n"
            f"🎯 Raydium | MCap {fmt(MIN_MCAP)}-{fmt(MAX_MCAP)}\n"
            f"💰 Per trade: {BUY_AMOUNT_SOL} SOL (${BUY_AMOUNT_SOL*sol_price:.0f})\n"
            f"📊 Max trades: {MAX_TRADES}\n"
            f"📉 Trailing stop: -{TRAILING_STOP*100:.0f}% från topp\n"
            f"🎯 TP1: +100% | TP2: +300% | TP3: +700%\n"
            f"💧 Min liq: {fmt(MIN_LIQ)}\n"
            f"👛 Dev max 10% | Top10 max 50%\n"
            f"📈 Min {MIN_BUYS_1H} köp/timme\n\n"
            f"⚡ _PumpScan Bot v5_"
        )
        await asyncio.gather(
            dex_scan_loop(session),
            monitor_trades(session),
        )

if __name__ == "__main__":
    asyncio.run(main())
