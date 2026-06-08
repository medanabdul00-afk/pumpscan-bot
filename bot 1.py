import asyncio
import aiohttp
import logging
import os
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
TG_TOKEN   = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT    = os.getenv("TG_CHAT",  "5667140911")
SCAN_EVERY = int(os.getenv("SCAN_EVERY", "60"))   # seconds between scans

# Quality filters
MIN_LIQ        = 10_000   # $10K min liquidity
MIN_VOL_1H     = 2_000    # $2K min volume last hour
MIN_HOLDERS    = 20       # min holders
MIN_SCORE      = 70       # min RugCheck score
MIN_LP_LOCKED  = 80       # min % LP locked
BUY_SELL_RATIO = 1.3      # buyers must be 1.3x sellers
MAX_AGE_H      = 24       # max age in hours
MIN_AGE_MIN    = 5        # min age in minutes

# ── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("pumpscan")

# ── STATE ────────────────────────────────────────────────────────────────────
notified = set()   # addresses already sent to Telegram

# ── HELPERS ─────────────────────────────────────────────────────────────────
def fmt(n):
    if n is None: return "N/A"
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000:     return f"${n/1_000:.1f}K"
    return f"${n:.0f}"

def age_minutes(pair):
    ts = pair.get("pairCreatedAt")
    if not ts: return 9999
    return (datetime.utcnow().timestamp()*1000 - ts) / 60_000

def age_hours(pair):
    return age_minutes(pair) / 60

# ── DEXSCREENER ─────────────────────────────────────────────────────────────
async def fetch_trending(session):
    """Fetch trending + latest Solana pairs from Dexscreener."""
    pairs = []
    endpoints = [
        "https://api.dexscreener.com/latest/dex/search?q=solana",
        "https://api.dexscreener.com/latest/dex/search?q=pump",
        "https://api.dexscreener.com/latest/dex/search?q=raydium",
        "https://api.dexscreener.com/token-boosts/latest/v1",
    ]
    for url in endpoints:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json(content_type=None)
                if isinstance(data, list):
                    for item in data:
                        if item.get("chainId") == "solana" or item.get("chain") == "solana":
                            pairs.append(item)
                else:
                    for p in data.get("pairs", []):
                        if p.get("chainId") == "solana":
                            pairs.append(p)
        except Exception as e:
            log.warning(f"Dexscreener fetch error ({url}): {e}")

    # Deduplicate by address
    seen = set()
    unique = []
    for p in pairs:
        addr = p.get("baseToken", {}).get("address") or p.get("tokenAddress", "")
        if addr and addr not in seen:
            seen.add(addr)
            unique.append(p)
    return unique

# ── RUGCHECK ────────────────────────────────────────────────────────────────
async def fetch_rugcheck(session, addr):
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            d = await r.json(content_type=None)

        # Normalize score to 0-100
        score = 0
        if isinstance(d.get("score"), (int, float)):
            score = min(100, max(0, round(d["score"])))
        elif isinstance(d.get("score_normalised"), (int, float)):
            score = min(100, max(0, round(d["score_normalised"])))
        elif isinstance(d.get("risks"), list):
            score = max(0, 100 - len(d["risks"]) * 12)

        # LP locked %
        lp_pct = 0
        if isinstance(d.get("lpLockedPct"), (int, float)):
            lp_pct = min(100, round(d["lpLockedPct"]))
        elif d.get("markets") and isinstance(d["markets"][0].get("lpLockedPct"), (int, float)):
            lp_pct = min(100, round(d["markets"][0]["lpLockedPct"]))

        # Creator sold
        creator_sold = (
            d.get("creatorBalance") == "SOLD" or
            (isinstance(d.get("creatorTokens"), (int, float)) and d["creatorTokens"] == 0)
        )

        return {"score": score, "lp_pct": lp_pct, "creator_sold": creator_sold}
    except Exception as e:
        log.warning(f"RugCheck error ({addr[:8]}...): {e}")
        return None

# ── FILTER ───────────────────────────────────────────────────────────────────
def apply_filters(pair, rc):
    """Return (verdict, reasons_pass, reasons_fail)"""
    name   = pair.get("baseToken", {}).get("name", "?")
    liq    = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol1h  = (pair.get("volume") or {}).get("h1", 0) or 0
    buys   = (pair.get("txns") or {}).get("h1", {}).get("buys", 0) or 0
    sells  = (pair.get("txns") or {}).get("h1", {}).get("sells", 1) or 1
    mcap   = pair.get("fdv") or pair.get("marketCap") or 0
    age_m  = age_minutes(pair)
    age_h  = age_m / 60

    score      = rc["score"]    if rc else 0
    lp_pct     = rc["lp_pct"]  if rc else 0
    creator_sold = rc["creator_sold"] if rc else False
    ratio      = buys / max(sells, 1)

    passes = []
    fails  = []

    # Age
    if MIN_AGE_MIN <= age_m <= MAX_AGE_H * 60:
        passes.append(f"⏱ Ålder {age_m:.0f} min")
    else:
        fails.append(f"⏱ Ålder {age_m:.0f} min (utanför 5min–24h)")

    # Liquidity
    if liq >= MIN_LIQ:
        passes.append(f"💧 Liq {fmt(liq)}")
    else:
        fails.append(f"💧 Liq {fmt(liq)} (behöver ${MIN_LIQ//1000}K+)")

    # Volume
    if vol1h >= MIN_VOL_1H:
        passes.append(f"📈 Vol 1h {fmt(vol1h)}")
    else:
        fails.append(f"📈 Vol 1h {fmt(vol1h)} (behöver ${MIN_VOL_1H//1000}K+)")

    # Buy/sell ratio
    if ratio >= BUY_SELL_RATIO:
        passes.append(f"🟢 Köp/sälj {ratio:.1f}x")
    else:
        fails.append(f"🔴 Köp/sälj {ratio:.1f}x (behöver {BUY_SELL_RATIO}x+)")

    # RugCheck
    if score >= MIN_SCORE:
        passes.append(f"🛡 Score {score}/100")
    else:
        fails.append(f"🛡 Score {score}/100 (behöver {MIN_SCORE}+)")

    # LP locked
    if lp_pct >= MIN_LP_LOCKED:
        passes.append(f"🔒 LP {lp_pct}%")
    else:
        fails.append(f"🔓 LP {lp_pct}% (behöver {MIN_LP_LOCKED}%+)")

    # Creator
    if creator_sold:
        passes.append("✅ Creator SOLD")
    else:
        fails.append("⚠️ Creator håller tokens")

    # Verdict
    critical_pass = liq >= MIN_LIQ and score >= MIN_SCORE and lp_pct >= MIN_LP_LOCKED
    good_ratio    = ratio >= BUY_SELL_RATIO
    good_vol      = vol1h >= MIN_VOL_1H
    right_age     = MIN_AGE_MIN <= age_m <= MAX_AGE_H * 60

    if critical_pass and good_ratio and good_vol and right_age and creator_sold:
        verdict = "GO"
    elif critical_pass and right_age:
        verdict = "WARN"
    else:
        verdict = "NOGO"

    return verdict, passes, fails

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
async def send_telegram(session, text):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                log.info("✅ Telegram skickad")
            else:
                body = await r.text()
                log.warning(f"Telegram fel {r.status}: {body}")
    except Exception as e:
        log.warning(f"Telegram exception: {e}")

async def notify(session, pair, rc, verdict, passes, fails):
    addr    = pair.get("baseToken", {}).get("address", "")
    name    = pair.get("baseToken", {}).get("name", "?")
    ticker  = pair.get("baseToken", {}).get("symbol", "?")
    liq     = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol1h   = (pair.get("volume") or {}).get("h1", 0) or 0
    vol24h  = (pair.get("volume") or {}).get("h24", 0) or 0
    mcap    = pair.get("fdv") or pair.get("marketCap") or 0
    age_m   = age_minutes(pair)
    score   = rc["score"]  if rc else 0
    lp_pct  = rc["lp_pct"] if rc else 0
    buys    = (pair.get("txns") or {}).get("h1", {}).get("buys", 0) or 0
    sells   = (pair.get("txns") or {}).get("h1", {}).get("sells", 0) or 0
    pct_1h  = (pair.get("priceChange") or {}).get("h1", 0) or 0
    pct_24h = (pair.get("priceChange") or {}).get("h24", 0) or 0

    emoji = "🔥" if verdict == "GO" else "⚠️"
    trend = "+" if pct_1h >= 0 else ""

    passes_txt = "\n".join(passes)
    fails_txt  = "\n".join(fails) if fails else "Inga"

    msg = (
        f"{emoji} *{verdict}: {name}* (${ticker})\n"
        f"⏱ {age_m:.0f} min gammal\n\n"
        f"💰 MCap: {fmt(mcap)}\n"
        f"💧 Liq: {fmt(liq)}\n"
        f"📈 Vol 1h: {fmt(vol1h)} | 24h: {fmt(vol24h)}\n"
        f"📊 Pris: {trend}{pct_1h:.1f}% (1h) | {pct_24h:.1f}% (24h)\n"
        f"🔄 Buys/Sells: {buys}/{sells}\n"
        f"🛡 RugCheck: {score}/100 | LP: {lp_pct}%\n\n"
        f"*Klarar:*\n{passes_txt}\n\n"
        f"*Missar:*\n{fails_txt}\n\n"
        f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n"
        f"🔍 [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"🟣 [Pump.fun](https://pump.fun/{addr})\n\n"
        f"⚡ _PumpScan Bot_"
    )
    await send_telegram(session, msg)

# ── MAIN SCAN LOOP ───────────────────────────────────────────────────────────
async def scan_once(session):
    log.info("🔍 Startar scan...")
    pairs = await fetch_trending(session)
    log.info(f"📦 {len(pairs)} Solana-pairs hittade")

    results = {"GO": 0, "WARN": 0, "NOGO": 0, "skip": 0}

    for pair in pairs:
        addr = pair.get("baseToken", {}).get("address") or pair.get("tokenAddress", "")
        if not addr or addr in notified:
            results["skip"] += 1
            continue

        # Quick pre-filter before RugCheck (save API calls)
        liq   = (pair.get("liquidity") or {}).get("usd", 0) or 0
        vol1h = (pair.get("volume") or {}).get("h1", 0) or 0
        age_m = age_minutes(pair)

        if liq < MIN_LIQ * 0.5 or age_m > MAX_AGE_H * 60 or age_m < MIN_AGE_MIN:
            results["skip"] += 1
            continue

        # RugCheck
        name = pair.get("baseToken", {}).get("name", "?")
        log.info(f"🛡 RugCheck: {name} ({addr[:8]}...)")
        rc = await fetch_rugcheck(session, addr)
        await asyncio.sleep(0.5)  # rate limit

        verdict, passes, fails = apply_filters(pair, rc)
        results[verdict] = results.get(verdict, 0) + 1

        log.info(f"  → {verdict}: {name} | score={rc['score'] if rc else '?'} lp={rc['lp_pct'] if rc else '?'}%")

        if verdict in ("GO", "WARN"):
            notified.add(addr)
            await notify(session, pair, rc, verdict, passes, fails)
            await asyncio.sleep(1)

    log.info(f"✅ Scan klar — GO:{results['GO']} WARN:{results['WARN']} NOGO:{results['NOGO']} Skip:{results['skip']}")

async def main():
    log.info("🚀 PumpScan Bot startar...")
    await send_startup_message()

    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                await scan_once(session)
            except Exception as e:
                log.error(f"Scan error: {e}")
            log.info(f"⏳ Väntar {SCAN_EVERY}s till nästa scan...")
            await asyncio.sleep(SCAN_EVERY)

async def send_startup_message():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as s:
        msg = (
            "🤖 *PumpScan Bot startad!*\n\n"
            f"🔍 Scannar Solana var {SCAN_EVERY}s\n"
            f"💧 Min liq: {fmt(MIN_LIQ)}\n"
            f"🛡 Min RugCheck: {MIN_SCORE}\n"
            f"🔒 Min LP Locked: {MIN_LP_LOCKED}%\n"
            f"⏱ Ålder: {MIN_AGE_MIN}min – {MAX_AGE_H}h\n"
            f"🟢 Köp/sälj ratio: {BUY_SELL_RATIO}x+\n\n"
            "Notiser skickas vid GO och WARN coins.\n"
            "⚡ _PumpScan Bot_"
        )
        await send_telegram(s, msg)

if __name__ == "__main__":
    asyncio.run(main())
