import asyncio
import aiohttp
import logging
import os
import time
from datetime import datetime, timezone
 
# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN   = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT    = os.getenv("TG_CHAT",  "5667140911")
SCAN_EVERY = int(os.getenv("SCAN_EVERY", "60"))
 
# Filters - balanced for real coins
MIN_LIQ        = 5_000    # $5K liquidity
MIN_VOL_1H     = 500      # $500 vol last hour
MIN_SCORE      = 40       # rugcheck score
MIN_LP_LOCKED  = 50       # % LP locked
BUY_SELL_RATIO = 1.1      # buyers > sellers
MAX_AGE_H      = 12       # max 12h old
MIN_AGE_MIN    = 2        # min 2 min old
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pumpscan")
 
# State
notified = set()  # never notify same coin twice
last_checked = {}  # addr -> timestamp
 
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
 
async def fetch_pairs(session):
    """Fetch from multiple Dexscreener endpoints to get diverse coins."""
    all_pairs = []
    
    # Multiple search queries to get different coins
    searches = [
        "https://api.dexscreener.com/latest/dex/search?q=pump.fun",
        "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
        "https://api.dexscreener.com/latest/dex/search?q=solana meme",
        "https://api.dexscreener.com/latest/dex/search?q=sol token",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]
    
    for url in searches:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: continue
                d = await r.json(content_type=None)
                if isinstance(d, list):
                    for item in d:
                        chain = item.get("chainId") or item.get("chain", "")
                        if chain == "solana":
                            # Token profile - need to fetch pair data
                            addr = item.get("tokenAddress") or item.get("address", "")
                            if addr:
                                try:
                                    async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=aiohttp.ClientTimeout(total=8)) as r2:
                                        if r2.status == 200:
                                            d2 = await r2.json(content_type=None)
                                            all_pairs.extend([p for p in (d2.get("pairs") or []) if p.get("chainId") == "solana"])
                                except: pass
                else:
                    pairs = d.get("pairs") or []
                    all_pairs.extend([p for p in pairs if p.get("chainId") == "solana"])
        except Exception as e:
            log.warning(f"Fetch error {url}: {e}")
        await asyncio.sleep(0.3)
 
    # Deduplicate
    seen = set()
    unique = []
    for p in all_pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        if addr and addr not in seen:
            seen.add(addr)
            unique.append(p)
    
    return unique
 
async def scan(session):
    log.info("🔍 Startar scan...")
    pairs = await fetch_pairs(session)
    log.info(f"📦 {len(pairs)} unika Solana-pairs")
 
    now = time.time()
    fresh = []
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        if not addr or addr in notified:
            continue
        # Only re-check after 20 min
        if now - last_checked.get(addr, 0) < 1200:
            continue
        
        age = age_min(p.get("pairCreatedAt"))
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        vol = (p.get("volume") or {}).get("h1", 0) or 0
        
        # Basic pre-filter
        if age < MIN_AGE_MIN or age > MAX_AGE_H * 60:
            continue
        if liq < MIN_LIQ:
            continue
            
        fresh.append(p)
        last_checked[addr] = now
 
    log.info(f"⏱ {len(fresh)} coins inom filter (ålder+liq)")
    
    if not fresh:
        log.info("ℹ Inga nya coins att kolla — samma coins returneras av API")
        return
 
    # Sort by volume desc, take top 8
    fresh.sort(key=lambda p: (p.get("volume") or {}).get("h1", 0) or 0, reverse=True)
    to_check = fresh[:8]
 
    go_count = 0
    for i, p in enumerate(to_check):
        addr = (p.get("baseToken") or {}).get("address", "")
        name = (p.get("baseToken") or {}).get("name", "?")
        ticker = (p.get("baseToken") or {}).get("symbol", "?")
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        vol1h = (p.get("volume") or {}).get("h1", 0) or 0
        vol24h = (p.get("volume") or {}).get("h24", 0) or 0
        buys = (p.get("txns") or {}).get("h1", {}).get("buys", 0) or 0
        sells = (p.get("txns") or {}).get("h1", {}).get("sells", 1) or 1
        mcap = p.get("fdv") or p.get("marketCap") or 0
        age = age_min(p.get("pairCreatedAt"))
        pct1h = (p.get("priceChange") or {}).get("h1", 0) or 0
        pct24h = (p.get("priceChange") or {}).get("h24", 0) or 0
        ratio = buys / max(sells, 1)
 
        log.info(f"🛡 [{i+1}/{len(to_check)}] RugCheck: {name} (liq={fmt(liq)} vol={fmt(vol1h)} age={age:.0f}min)")
        rc = await get_rugcheck(session, addr)
        await asyncio.sleep(0.5)
 
        score = rc["score"] if rc else 0
        lp = rc["lp"] if rc else 0
        creator_sold = rc["creator_sold"] if rc else False
 
        log.info(f"  → score={score} lp={lp}% ratio={ratio:.1f}x age={age:.0f}min")
 
        # Verdict
        if score >= MIN_SCORE and lp >= MIN_LP_LOCKED and ratio >= BUY_SELL_RATIO and vol1h >= MIN_VOL_1H:
            verdict = "🔥 GO" if score >= 70 else "✅ GO"
        elif score >= MIN_SCORE and ratio >= BUY_SELL_RATIO:
            verdict = "⚠️ WARN"
        else:
            log.info(f"  → NOGO (score:{score}<{MIN_SCORE} OR lp:{lp}<{MIN_LP_LOCKED} OR ratio:{ratio:.1f}<{BUY_SELL_RATIO})")
            continue
 
        # Send notification
        notified.add(addr)
        go_count += 1
        trend = "+" if pct1h >= 0 else ""
 
        passes = []
        fails = []
        if score >= MIN_SCORE: passes.append(f"✓ Score {score}/100")
        else: fails.append(f"✗ Score {score}/100")
        if lp >= MIN_LP_LOCKED: passes.append(f"✓ LP {lp}%")
        else: fails.append(f"✗ LP {lp}%")
        if ratio >= BUY_SELL_RATIO: passes.append(f"✓ Köp/sälj {ratio:.1f}x")
        else: fails.append(f"✗ Ratio {ratio:.1f}x")
        if creator_sold: passes.append("✓ Creator SOLD")
        else: fails.append("⚠ Creator håller")
 
        msg = (
            f"{verdict}: *{name}* (${ticker})\n"
            f"⏱ {age:.0f} min gammal\n\n"
            f"💰 MCap: {fmt(mcap)}\n"
            f"💧 Liq: {fmt(liq)}\n"
            f"📈 Vol 1h: {fmt(vol1h)} | 24h: {fmt(vol24h)}\n"
            f"📊 Pris: {trend}{pct1h:.1f}% (1h) | {pct24h:.1f}% (24h)\n"
            f"🔄 Buys/Sells 1h: {buys}/{sells}\n"
            f"🛡 Score: {score}/100 | LP: {lp}%\n\n"
            + ("\n".join(passes + fails)) +
            f"\n\n🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n"
            f"🔍 [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
            f"🟣 [Pump.fun](https://pump.fun/{addr})\n\n"
            f"⚡ _PumpScan Bot_"
        )
        await send_tg(session, msg)
        log.info(f"  → {verdict} notis skickad för {name}!")
 
    log.info(f"✅ Scan klar — {go_count} GO/WARN av {len(to_check)} kollade")
 
async def main():
    log.info("🚀 PumpScan Bot v2 startar...")
    conn = aiohttp.TCPConnector(limit=5, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        await send_tg(session,
            "🤖 *PumpScan Bot v2 startad!*\n\n"
            f"🔍 Scannar var {SCAN_EVERY}s\n"
            f"💧 Min liq: {fmt(MIN_LIQ)}\n"
            f"🛡 Min score: {MIN_SCORE}\n"
            f"🔒 Min LP: {MIN_LP_LOCKED}%\n"
            f"⏱ Ålder: {MIN_AGE_MIN}min–{MAX_AGE_H}h\n"
            f"🟢 Ratio: {BUY_SELL_RATIO}x+\n\n"
            "⚡ _PumpScan Bot_"
        )
        while True:
            try:
                await scan(session)
            except Exception as e:
                log.error(f"Scan fel: {e}")
            log.info(f"⏳ Väntar {SCAN_EVERY}s...")
            await asyncio.sleep(SCAN_EVERY)
 
if __name__ == "__main__":
    asyncio.run(main())
 
