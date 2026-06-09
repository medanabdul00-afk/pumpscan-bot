import asyncio
import aiohttp
import logging
import os
import time
import json

# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN     = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT      = os.getenv("TG_CHAT",  "5667140911")
HELIUS_KEY   = os.getenv("HELIUS_KEY", "85dee6a1-d8e2-421e-8a26-33645c4a943f")
SCAN_EVERY   = int(os.getenv("SCAN_EVERY", "45"))

# Filters
MIN_LIQ        = 3_000
MIN_VOL_1H     = 300
MIN_SCORE      = 40
MIN_LP_LOCKED  = 50
BUY_SELL_RATIO = 1.1
MAX_AGE_H      = 6
MIN_AGE_MIN    = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pumpscan")

notified   = set()
last_check = {}

def fmt(n):
    if not n: return "N/A"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    if n >= 1e3: return f"${n/1e3:.1f}K"
    return f"${n:.0f}"

def age_min(ts):
    if not ts: return 9999
    t = ts if isinstance(ts, (int, float)) else 0
    return (time.time()*1000 - t) / 60_000

def momentum_signal(pct5m, pct1h, buys, sells, vol1h):
    signals = []
    score = 0
    if pct5m > 5:
        signals.append(f"📈 +{pct5m:.1f}% senaste 5 min — starkt momentum")
        score += 2
    elif pct5m > 0:
        signals.append(f"📈 +{pct5m:.1f}% senaste 5 min")
        score += 1
    elif pct5m < -10:
        signals.append(f"📉 {pct5m:.1f}% senaste 5 min — varning!")
        score -= 2
    elif pct5m < 0:
        signals.append(f"📉 {pct5m:.1f}% senaste 5 min")
        score -= 1

    ratio = buys / max(sells, 1)
    if ratio >= 2:
        signals.append(f"🟢 {buys} köp vs {sells} sälj — starkt köptryck ({ratio:.1f}x)")
        score += 2
    elif ratio >= 1.3:
        signals.append(f"🟢 {buys} köp vs {sells} sälj — bra ratio ({ratio:.1f}x)")
        score += 1
    elif ratio < 0.8:
        signals.append(f"🔴 {buys} köp vs {sells} sälj — säljtryck!")
        score -= 2
    else:
        signals.append(f"🟡 {buys} köp vs {sells} sälj — neutralt ({ratio:.1f}x)")

    if vol1h >= 50_000:
        signals.append(f"🔥 Vol {fmt(vol1h)} — mycket aktiv")
        score += 2
    elif vol1h >= 10_000:
        signals.append(f"✅ Vol {fmt(vol1h)} — aktiv")
        score += 1
    elif vol1h < 500:
        signals.append(f"⚠️ Vol {fmt(vol1h)} — låg aktivitet")
        score -= 1

    if score >= 4:
        verdict = "🔥 STARKT BULLISH — bra entry just nu"
    elif score >= 2:
        verdict = "✅ BULLISH — rimlig entry"
    elif score >= 0:
        verdict = "🟡 NEUTRAL — vänta och se"
    else:
        verdict = "🔴 BEARISH — undvik just nu"

    return verdict, signals

async def send_tg(session, msg):
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("📱 Telegram skickad!")
            else:
                log.warning(f"TG fel {r.status}: {await r.text()}")
    except Exception as e:
        log.warning(f"TG exception: {e}")

async def get_rugcheck(session, addr):
    try:
        async with session.get(
            f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)
        score = 0
        if isinstance(d.get("score"), (int, float)):
            score = min(100, max(0, round(d["score"])))
        elif isinstance(d.get("score_normalised"), (int, float)):
            score = min(100, max(0, round(d["score_normalised"])))
        elif isinstance(d.get("risks"), list):
            score = max(0, 100 - len(d["risks"]) * 10)
        lp = 0
        if isinstance(d.get("lpLockedPct"), (int, float)):
            lp = min(100, round(d["lpLockedPct"]))
        elif d.get("markets") and isinstance(d["markets"][0].get("lpLockedPct"), (int, float)):
            lp = min(100, round(d["markets"][0]["lpLockedPct"]))
        creator_sold = d.get("creatorBalance") == "SOLD"
        return {"score": score, "lp": lp, "creator_sold": creator_sold}
    except:
        return None

async def get_pair_for_token(session, addr):
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            d = await r.json(content_type=None)
            pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs: return None
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
    except:
        return None

async def analyze_and_notify(session, pair):
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
    pct24h = (pair.get("priceChange") or {}).get("h24", 0) or 0
    ratio  = buys / max(sells, 1)

    if age < MIN_AGE_MIN or age > MAX_AGE_H * 60: return
    if liq < MIN_LIQ or vol1h < MIN_VOL_1H: return

    log.info(f"🛡 RugCheck: {name} age={age:.0f}min liq={fmt(liq)} vol={fmt(vol1h)}")
    rc = await get_rugcheck(session, addr)
    await asyncio.sleep(0.5)

    score = rc["score"] if rc else 0
    lp    = rc["lp"] if rc else 0
    creator_sold = rc["creator_sold"] if rc else False

    log.info(f"  → score={score} lp={lp}% ratio={ratio:.1f}x")

    if score >= MIN_SCORE and lp >= MIN_LP_LOCKED and ratio >= BUY_SELL_RATIO:
        verdict = "🔥 GO" if score >= 70 else "✅ GO"
    elif score >= MIN_SCORE and ratio >= BUY_SELL_RATIO:
        verdict = "⚠️ WARN"
    else:
        log.info(f"  → NOGO (score:{score} lp:{lp}% ratio:{ratio:.1f}x)")
        return

    momentum_verdict, momentum_signals = momentum_signal(pct5m, pct1h, buys, sells, vol1h)

    checks = []
    if score >= MIN_SCORE: checks.append(f"✓ Score {score}/100")
    else: checks.append(f"✗ Score {score}/100")
    if lp >= MIN_LP_LOCKED: checks.append(f"✓ LP {lp}%")
    else: checks.append(f"✗ LP {lp}%")
    if ratio >= BUY_SELL_RATIO: checks.append(f"✓ Köp/sälj {ratio:.1f}x")
    else: checks.append(f"✗ Ratio {ratio:.1f}x")
    if creator_sold: checks.append("✓ Creator SOLD")
    else: checks.append("⚠ Creator håller")

    msg = (
        f"{verdict}: *{name}* (${ticker})\n"
        f"⏱ {age:.0f} min gammal\n\n"
        f"💰 MCap: {fmt(mcap)}\n"
        f"💧 Liq: {fmt(liq)}\n"
        f"📈 Vol 1h: {fmt(vol1h)} | 24h: {fmt(vol24h)}\n"
        f"📊 Pris 1h: {'+' if pct1h>=0 else ''}{pct1h:.1f}% | 5min: {'+' if pct5m>=0 else ''}{pct5m:.1f}%\n"
        f"🔄 Buys/Sells: {buys}/{sells}\n"
        f"🛡 Score: {score}/100 | LP: {lp}%\n\n"
        f"📊 *CHARTANALYS:*\n"
        f"{momentum_verdict}\n"
        + "\n".join(momentum_signals) +
        f"\n\n🔐 *SÄKERHETSCHECK:*\n"
        + "\n".join(checks) +
        f"\n\n🎯 Entry: Nu om momentum håller\n"
        f"🛑 Stop loss: −20%\n"
        f"💰 Target: +50–100%\n\n"
        f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n"
        f"🔍 [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"🟣 [Pump.fun](https://pump.fun/{addr})\n\n"
        f"⚡ _PumpScan Bot v3_"
    )

    notified.add(addr)
    await send_tg(session, msg)
    log.info(f"  → {verdict} skickad för {name}!")

async def helius_new_tokens(session):
    """Use Helius to get newly created tokens via enhanced API."""
    url = f"https://api.helius.xyz/v0/addresses/TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA/transactions?api-key={HELIUS_KEY}&type=CREATE_ACCOUNT&limit=20"
    while True:
        try:
            log.info("⚡ Helius — hämtar nya tokens...")
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    txns = await r.json(content_type=None)
                    log.info(f"  → {len(txns)} transaktioner från Helius")
                    for txn in txns:
                        for acc in txn.get("accountData", []):
                            addr = acc.get("account", "")
                            if addr and addr not in notified and addr not in last_check:
                                last_check[addr] = time.time()
                                await asyncio.sleep(20)  # wait for liquidity
                                pair = await get_pair_for_token(session, addr)
                                if pair:
                                    await analyze_and_notify(session, pair)
                else:
                    log.warning(f"Helius HTTP {r.status}")
        except Exception as e:
            log.warning(f"Helius fel: {e}")
        await asyncio.sleep(30)

async def dex_scan_loop(session):
    """Backup Dexscreener scan every 45s."""
    while True:
        try:
            log.info("🔍 Dexscreener scan...")
            searches = [
                "https://api.dexscreener.com/latest/dex/search?q=pump.fun",
                "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
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
            fresh = []
            for p in all_pairs:
                addr = (p.get("baseToken") or {}).get("address", "")
                if not addr or addr in seen or addr in notified: continue
                if now - last_check.get(addr, 0) < 1200: continue
                seen.add(addr)
                age = age_min(p.get("pairCreatedAt"))
                liq = (p.get("liquidity") or {}).get("usd", 0) or 0
                if MIN_AGE_MIN <= age <= MAX_AGE_H * 60 and liq >= MIN_LIQ:
                    fresh.append(p)
                    last_check[addr] = now

            log.info(f"📦 {len(fresh)} nya coins att analysera")
            fresh.sort(key=lambda p: (p.get("volume") or {}).get("h1", 0) or 0, reverse=True)
            for p in fresh[:6]:
                await analyze_and_notify(session, p)
                await asyncio.sleep(1)

        except Exception as e:
            log.error(f"Dex scan fel: {e}")
        await asyncio.sleep(SCAN_EVERY)

async def main():
    log.info("🚀 PumpScan Bot v3 startar...")
    conn = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        await send_tg(session,
            "🤖 *PumpScan Bot v3 startad!*\n\n"
            "⚡ Helius API — nya tokens snabbt\n"
            "📊 Automatisk chartanalys\n"
            "🔍 Dexscreener backup var 45s\n\n"
            f"💧 Min liq: {fmt(MIN_LIQ)}\n"
            f"🛡 Min score: {MIN_SCORE}\n"
            f"⏱ Ålder: {MIN_AGE_MIN}min–{MAX_AGE_H}h\n\n"
            "⚡ _PumpScan Bot v3_"
        )
        await asyncio.gather(
            helius_new_tokens(session),
            dex_scan_loop(session),
        )

if __name__ == "__main__":
    asyncio.run(main())
