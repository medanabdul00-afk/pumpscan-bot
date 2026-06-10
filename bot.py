import asyncio
import aiohttp
import logging
import os
import time
import json
import base58

# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN      = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT       = os.getenv("TG_CHAT",  "5667140911")
HELIUS_KEY    = os.getenv("HELIUS_KEY", "85dee6a1-d8e2-421e-8a26-33645c4a943f")
WALLET_KEY    = os.getenv("WALLET_PRIVATE_KEY", "")
SCAN_EVERY    = int(os.getenv("SCAN_EVERY", "45"))

# Trading config
BUY_AMOUNT_SOL  = float(os.getenv("BUY_AMOUNT_SOL", "0.05"))   # SOL per trade (~$10)
MAX_TRADES      = int(os.getenv("MAX_TRADES", "3"))              # max simultaneous trades
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "0.25")) # max 0.25 SOL loss per day
TAKE_PROFIT_1   = float(os.getenv("TAKE_PROFIT_1", "1.0"))      # sell 50% at +100%
TAKE_PROFIT_2   = float(os.getenv("TAKE_PROFIT_2", "2.0"))      # sell rest at +200%
STOP_LOSS       = float(os.getenv("STOP_LOSS", "0.20"))         # stop loss at -20%

# Market filters
MIN_LIQ        = 5_000
MIN_VOL_1H     = 500
BUY_SELL_RATIO = 1.5
MAX_AGE_H      = 2
MIN_AGE_MIN    = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pumpscan")

# State
notified     = set()
last_check   = {}
active_trades = {}   # addr -> {buy_price, amount_sol, buy_time, tp1_done}
daily_loss   = 0.0
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
        return 180.0  # fallback

async def buy_token(session, token_addr, token_symbol):
    """Buy token via Jupiter API."""
    global daily_loss

    if not WALLET_KEY:
        log.warning("Ingen wallet key!")
        return None

    if len(active_trades) >= MAX_TRADES:
        log.info(f"Max trades ({MAX_TRADES}) nådda — skippar köp")
        return None

    if daily_loss >= DAILY_LOSS_LIMIT:
        log.warning(f"Daglig förlustgräns nådd ({daily_loss:.3f} SOL) — stannar för idag")
        await send_tg(session, 
            f"🛑 *Daglig förlustgräns nådd!*\n"
            f"Förlorat {daily_loss:.3f} SOL idag.\n"
            f"Boten pausar trading tills imorgon. 🔒"
        )
        return None

    try:
        sol_price = await get_sol_price(session)
        buy_lamports = int(BUY_AMOUNT_SOL * 1e9)
        
        # WSOL mint address
        WSOL = "So11111111111111111111111111111111111111112"
        
        log.info(f"💰 Köper {token_symbol} för {BUY_AMOUNT_SOL} SOL...")
        
        # Get quote from Jupiter
        async with session.get(
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={WSOL}"
            f"&outputMint={token_addr}"
            f"&amount={buy_lamports}"
            f"&slippageBps=2000",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"Jupiter quote fel: {r.status}")
                return None
            quote = await r.json(content_type=None)

        if not quote or "outAmount" not in quote:
            log.warning("Ingen quote från Jupiter")
            return None

        out_amount = int(quote["outAmount"])
        log.info(f"  → Får {out_amount} tokens för {BUY_AMOUNT_SOL} SOL")

        # Get swap transaction
        swap_body = {
            "quoteResponse": quote,
            "userPublicKey": get_public_key(WALLET_KEY),
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": 50000,
        }

        async with session.post(
            "https://quote-api.jup.ag/v6/swap",
            json=swap_body,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                log.warning(f"Jupiter swap fel: {r.status} — {await r.text()}")
                return None
            swap_data = await r.json(content_type=None)

        if not swap_data.get("swapTransaction"):
            log.warning("Ingen swap transaction från Jupiter")
            return None

        # Sign and send transaction via Helius
        tx_base64 = swap_data["swapTransaction"]
        
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    tx_base64,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "maxRetries": 3
                    }
                ]
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            result = await r.json(content_type=None)
            
            if result.get("error"):
                log.warning(f"Send TX fel: {result['error']}")
                return None
                
            tx_sig = result.get("result")
            if not tx_sig:
                log.warning("Ingen TX signature")
                return None

        log.info(f"  ✅ Köpt! TX: {tx_sig[:20]}...")
        
        # Get current price for tracking
        buy_price = await get_token_price(session, token_addr)
        
        trade = {
            "buy_price": buy_price,
            "buy_price_usd": buy_price * sol_price if buy_price else 0,
            "amount_sol": BUY_AMOUNT_SOL,
            "buy_time": time.time(),
            "tp1_done": False,
            "symbol": token_symbol,
            "tx": tx_sig,
        }
        active_trades[token_addr] = trade
        
        return tx_sig

    except Exception as e:
        log.error(f"Kjøp-fel: {e}")
        return None

async def sell_token(session, token_addr, reason="manual", pct=100):
    """Sell token via Jupiter API."""
    global daily_loss
    
    if token_addr not in active_trades:
        return None
    
    trade = active_trades[token_addr]
    
    try:
        WSOL = "So11111111111111111111111111111111111111112"
        
        # Get token balance
        balance = await get_token_balance(session, token_addr)
        if not balance or balance == 0:
            log.warning(f"Ingen balance för {token_addr[:12]}")
            active_trades.pop(token_addr, None)
            return None

        sell_amount = int(balance * (pct / 100))
        if sell_amount == 0:
            return None

        log.info(f"💸 Säljer {pct}% av {trade['symbol']} — anledning: {reason}")

        # Get quote
        async with session.get(
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={token_addr}"
            f"&outputMint={WSOL}"
            f"&amount={sell_amount}"
            f"&slippageBps=2500",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"Sell quote fel: {r.status}")
                return None
            quote = await r.json(content_type=None)

        if not quote:
            return None

        swap_body = {
            "quoteResponse": quote,
            "userPublicKey": get_public_key(WALLET_KEY),
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": 50000,
        }

        async with session.post(
            "https://quote-api.jup.ag/v6/swap",
            json=swap_body,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                return None
            swap_data = await r.json(content_type=None)

        if not swap_data.get("swapTransaction"):
            return None

        tx_base64 = swap_data["swapTransaction"]
        
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [tx_base64, {"encoding": "base64", "skipPreflight": False}]
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            result = await r.json(content_type=None)
            tx_sig = result.get("result")

        if pct == 100:
            active_trades.pop(token_addr, None)

        return tx_sig

    except Exception as e:
        log.error(f"Sälj-fel: {e}")
        return None

async def get_token_price(session, token_addr):
    """Get current token price in SOL."""
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
    """Get token balance from wallet."""
    try:
        pub_key = get_public_key(WALLET_KEY)
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    pub_key,
                    {"mint": token_addr},
                    {"encoding": "jsonParsed"}
                ]
            },
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            result = await r.json(content_type=None)
            accounts = result.get("result", {}).get("value", [])
            if not accounts: return 0
            amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            return int(amount)
    except:
        return 0

def get_public_key(private_key_str):
    """Get public key from private key."""
    try:
        from solders.keypair import Keypair
        if len(private_key_str) > 50 and not private_key_str.startswith("["):
            key_bytes = base58.b58decode(private_key_str)
        else:
            key_bytes = bytes(json.loads(private_key_str))
        kp = Keypair.from_bytes(key_bytes)
        return str(kp.pubkey())
    except Exception as e:
        log.error(f"Public key fel: {e}")
        return ""

async def monitor_trades(session):
    """Monitor active trades and execute stop loss / take profit."""
    while True:
        try:
            for addr in list(active_trades.keys()):
                trade = active_trades.get(addr)
                if not trade: continue

                current_price = await get_token_price(session, addr)
                if not current_price or not trade["buy_price"]:
                    continue

                pct_change = (current_price - trade["buy_price"]) / trade["buy_price"]
                symbol = trade["symbol"]

                log.info(f"📊 {symbol}: {pct_change*100:.1f}% ({'+' if pct_change>=0 else ''}{pct_change*100:.1f}%)")

                # Stop loss -20%
                if pct_change <= -STOP_LOSS:
                    log.info(f"🛑 Stop loss triggered for {symbol}!")
                    tx = await sell_token(session, addr, reason="stop_loss", pct=100)
                    loss_sol = trade["amount_sol"] * STOP_LOSS
                    global daily_loss
                    daily_loss += loss_sol
                    await send_tg(session,
                        f"🛑 *STOP LOSS — {symbol}*\n"
                        f"Sålde vid {pct_change*100:.1f}%\n"
                        f"Förlorade ca {loss_sol:.3f} SOL\n"
                        f"Daglig förlust: {daily_loss:.3f} SOL\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                # Take profit 1 — sälj 50% vid +100%
                elif pct_change >= TAKE_PROFIT_1 and not trade["tp1_done"]:
                    log.info(f"🎯 Take profit 1 triggered for {symbol}!")
                    tx = await sell_token(session, addr, reason="take_profit_1", pct=50)
                    trade["tp1_done"] = True
                    profit_sol = trade["amount_sol"] * 0.5 * TAKE_PROFIT_1
                    await send_tg(session,
                        f"🎯 *TAKE PROFIT 50% — {symbol}*\n"
                        f"Sålde 50% vid +{pct_change*100:.0f}%\n"
                        f"Vinst: ca +{profit_sol:.3f} SOL 💰\n"
                        f"Håller 50% — target +200%\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                # Take profit 2 — sälj resten vid +200%
                elif pct_change >= TAKE_PROFIT_2 and trade["tp1_done"]:
                    log.info(f"🚀 Take profit 2 triggered for {symbol}!")
                    tx = await sell_token(session, addr, reason="take_profit_2", pct=100)
                    profit_sol = trade["amount_sol"] * TAKE_PROFIT_2
                    await send_tg(session,
                        f"🚀 *TAKE PROFIT 100% — {symbol}*\n"
                        f"Sålde resten vid +{pct_change*100:.0f}%\n"
                        f"Total vinst: ca +{profit_sol:.3f} SOL 💰🔥\n\n"
                        f"⚡ _PumpScan Bot_"
                    )

                await asyncio.sleep(2)

        except Exception as e:
            log.error(f"Monitor trades fel: {e}")

        await asyncio.sleep(30)  # check every 30 seconds

async def full_rugcheck(session, addr):
    try:
        async with session.get(
            f"https://api.rugcheck.xyz/v1/tokens/{addr}/report",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)

        freeze_auth = d.get("freezeAuthority") or d.get("freeze_authority")
        has_freeze = freeze_auth is not None and freeze_auth != "" and freeze_auth != "null"
        mint_auth = d.get("mintAuthority") or d.get("mint_authority")
        has_mint = mint_auth is not None and mint_auth != "" and mint_auth != "null"
        mutable = d.get("mutableMetadata") or False
        top_holders = d.get("topHolders") or []
        max_holder_pct = 0
        if top_holders:
            non_lp = [h for h in top_holders if not h.get("isLpHolder")]
            if non_lp:
                max_holder_pct = max(h.get("pct", 0) for h in non_lp)
        lp_pct = 0
        markets = d.get("markets") or []
        if markets:
            lp_pct = min(100, round(float(markets[0].get("lpLockedPct") or 0)))
        score = 0
        if isinstance(d.get("score"), (int, float)):
            score = min(100, max(0, round(d["score"])))
        creator_sold = d.get("creatorBalance") == "SOLD"
        risks = d.get("risks") or []
        risk_names = [r.get("name", "").lower() for r in risks]
        is_honeypot = any("honeypot" in r for r in risk_names)
        has_freeze_risk = any("freeze" in r for r in risk_names)
        has_mint_risk = any("mint" in r for r in risk_names)
        high_risks = [r for r in risks if r.get("level", "").lower() == "danger"]

        return {
            "score": score,
            "lp_pct": lp_pct,
            "creator_sold": creator_sold,
            "freeze_authority": has_freeze or has_freeze_risk,
            "mint_authority": has_mint or has_mint_risk,
            "mutable_metadata": mutable,
            "max_holder_pct": max_holder_pct,
            "is_honeypot": is_honeypot,
            "high_risks": high_risks,
        }
    except Exception as e:
        log.warning(f"RugCheck fel: {e}")
        return None

def is_safe(rc):
    """Returns True only if coin passes ALL critical safety checks."""
    if rc["freeze_authority"]: return False
    if rc["mint_authority"]: return False
    if rc["is_honeypot"]: return False
    if rc["mutable_metadata"]: return False
    if rc["max_holder_pct"] > 30: return False
    if len(rc["high_risks"]) > 0: return False
    if rc["score"] < 30: return False
    return True

async def analyze_and_buy(session, pair):
    """Full analysis — if GO, buy automatically."""
    addr   = (pair.get("baseToken") or {}).get("address", "")
    if not addr or addr in notified: return

    name   = (pair.get("baseToken") or {}).get("name", "?")
    ticker = (pair.get("baseToken") or {}).get("symbol", "?")
    liq    = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol1h  = (pair.get("volume") or {}).get("h1", 0) or 0
    vol24h = (pair.get("volume") or {}).get("h24", 0) or 0
    buys   = (pair.get("txns") or {}).get("h1", {}).get("buys", 0) or 0
    sells  = (pair.get("txns") or {}).get("h1", {}).get("sells", 1) or 1
    mcap   = pair.get("fdv") or pair.get("marketCap") or 0
    age    = age_min(pair.get("pairCreatedAt"))
    pct5m  = (pair.get("priceChange") or {}).get("m5", 0) or 0
    pct1h  = (pair.get("priceChange") or {}).get("h1", 0) or 0
    ratio  = buys / max(sells, 1)

    if age < MIN_AGE_MIN or age > MAX_AGE_H * 60: return
    if liq < MIN_LIQ or vol1h < MIN_VOL_1H: return
    if ratio < BUY_SELL_RATIO: return

    log.info(f"🔍 Analyserar: {name} age={age:.0f}min liq={fmt(liq)} ratio={ratio:.1f}x")

    rc = await full_rugcheck(session, addr)
    await asyncio.sleep(0.5)

    if not rc:
        log.info(f"  → Ingen RugCheck data — skippar")
        return

    if not is_safe(rc):
        log.info(f"  → NOGO säkerhet — freeze:{rc['freeze_authority']} mint:{rc['mint_authority']} honeypot:{rc['is_honeypot']}")
        return

    # Only send GO — no warnings
    notified.add(addr)
    log.info(f"  → ✅ GO! Köper {name}...")

    # Buy automatically
    tx_sig = await buy_token(session, addr, ticker)

    if tx_sig:
        sol_price = await get_sol_price(session)
        usd_amount = BUY_AMOUNT_SOL * sol_price
        await send_tg(session,
            f"🔥 *KÖPT: {name}* (${ticker})\n"
            f"⏱ {age:.0f} min gammal\n\n"
            f"💰 Investerat: {BUY_AMOUNT_SOL} SOL (${usd_amount:.0f})\n"
            f"💧 Liq: {fmt(liq)}\n"
            f"📈 Vol 1h: {fmt(vol1h)}\n"
            f"📊 +{pct5m:.1f}% (5min) | +{pct1h:.1f}% (1h)\n"
            f"🔄 Köp/sälj: {buys}/{sells} ({ratio:.1f}x)\n"
            f"🛡 Score: {rc['score']}/100\n\n"
            f"🎯 TP1: +100% (säljer 50%)\n"
            f"🎯 TP2: +200% (säljer resten)\n"
            f"🛑 Stop loss: -20%\n\n"
            f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n"
            f"🔍 [RugCheck](https://rugcheck.xyz/tokens/{addr})\n\n"
            f"⚡ _PumpScan Bot v5_"
        )
    else:
        await send_tg(session,
            f"⚠️ *GO men köp misslyckades: {name}*\n"
            f"Kolla manuellt!\n"
            f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n\n"
            f"⚡ _PumpScan Bot v5_"
        )

async def dex_scan_loop(session):
    while True:
        try:
            log.info("🔍 Dexscreener scan...")
            searches = [
                "https://api.dexscreener.com/latest/dex/search?q=pump.fun",
                "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
                "https://api.dexscreener.com/latest/dex/search?q=solana+meme",
                "https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-profiles/latest/v1",
            ]
            all_pairs = []
            for url in searches:
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
                                            async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=aiohttp.ClientTimeout(total=8)) as r2:
                                                if r2.status == 200:
                                                    d2 = await r2.json(content_type=None)
                                                    pairs = [p for p in (d2.get("pairs") or []) if p.get("chainId") == "solana"]
                                                    if pairs:
                                                        all_pairs.append(max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0))
                                        except: pass
                        else:
                            for p in (d.get("pairs") or []):
                                if p.get("chainId") == "solana":
                                    all_pairs.append(p)
                except Exception as e:
                    log.warning(f"Dex error: {e}")
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
                if MIN_AGE_MIN <= age <= MAX_AGE_H * 60 and liq >= MIN_LIQ:
                    fresh.append(p)
                    last_check[addr] = now

            log.info(f"📦 {len(fresh)} nya coins att analysera")
            fresh.sort(key=lambda p: (p.get("volume") or {}).get("h1", 0) or 0, reverse=True)
            for p in fresh[:6]:
                await analyze_and_buy(session, p)
                await asyncio.sleep(1)

        except Exception as e:
            log.error(f"Dex scan fel: {e}")
        await asyncio.sleep(SCAN_EVERY)

async def main():
    log.info("🚀 PumpScan Bot v5 (Auto-trading) startar...")
    conn = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        sol_price = await get_sol_price(session)
        await send_tg(session,
            f"🤖 *PumpScan Bot v5 startad!*\n\n"
            f"🤖 *AUTO-TRADING AKTIVT*\n"
            f"💰 Per trade: {BUY_AMOUNT_SOL} SOL (${BUY_AMOUNT_SOL*sol_price:.0f})\n"
            f"📊 Max trades: {MAX_TRADES} samtidigt\n"
            f"🛑 Stop loss: -{STOP_LOSS*100:.0f}%\n"
            f"🎯 TP1: +{TAKE_PROFIT_1*100:.0f}% (säljer 50%)\n"
            f"🎯 TP2: +{TAKE_PROFIT_2*100:.0f}% (säljer resten)\n"
            f"🔒 Daglig förlustgräns: {DAILY_LOSS_LIMIT} SOL\n\n"
            f"🛡 Säkerhetskontroller aktiva\n"
            f"📱 Du får notis vid varje köp och sälj\n\n"
            f"⚡ _PumpScan Bot v5_"
        )
        await asyncio.gather(
            dex_scan_loop(session),
            monitor_trades(session),
        )

if __name__ == "__main__":
    asyncio.run(main())
