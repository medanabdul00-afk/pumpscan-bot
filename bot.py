import asyncio
import aiohttp
import logging
import os
import time

# ── CONFIG ───────────────────────────────────────────────────────────────────
TG_TOKEN   = os.getenv("TG_TOKEN", "8908814441:AAGFGs52sINf_LjU6Mt6YP_yCcEZvQflhqM")
TG_CHAT    = os.getenv("TG_CHAT",  "5667140911")
HELIUS_KEY = os.getenv("HELIUS_KEY", "85dee6a1-d8e2-421e-8a26-33645c4a943f")
SCAN_EVERY = int(os.getenv("SCAN_EVERY", "45"))

# Market filters - Läge 2 (Balanserat)
MIN_LIQ        = 3_000    # $3K min liquidity
MIN_VOL_1H     = 300      # $300 vol last hour
BUY_SELL_RATIO = 1.1      # buyers > sellers
MAX_AGE_H      = 12       # max 12h old
MIN_AGE_MIN    = 2        # min 2 min old

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

async def send_tg(session, msg):
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"TG fel {r.status}: {await r.text()}")
            else:
                log.info("📱 Telegram skickad!")
    except Exception as e:
        log.warning(f"TG exception: {e}")

async def full_rugcheck(session, addr):
    """
    Fetch FULL RugCheck report and extract ALL safety flags.
    Returns dict with all critical safety data, or None on error.
    """
    try:
        # Use full report endpoint, not just summary
        async with session.get(
            f"https://api.rugcheck.xyz/v1/tokens/{addr}/report",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log.warning(f"RugCheck HTTP {r.status} for {addr[:12]}")
                return None
            d = await r.json(content_type=None)

        # ── CRITICAL SAFETY FLAGS ─────────────────────────────────────────
        # These alone = instant NOGO regardless of score

        # 1. Freeze authority — can someone freeze your tokens?
        freeze_auth = d.get("freezeAuthority") or d.get("freeze_authority")
        has_freeze = freeze_auth is not None and freeze_auth != "" and freeze_auth != "null"

        # 2. Mint authority — can creator print more tokens and dump?
        mint_auth = d.get("mintAuthority") or d.get("mint_authority")
        has_mint = mint_auth is not None and mint_auth != "" and mint_auth != "null"

        # 3. Mutable metadata — can contract be changed after deploy?
        mutable = d.get("mutableMetadata") or d.get("mutable_metadata") or False

        # 4. Top holder concentration — does one wallet own too much?
        top_holders = d.get("topHolders") or d.get("top_holders") or []
        max_holder_pct = 0
        if top_holders:
            # Exclude LP pool from calculation
            non_lp = [h for h in top_holders if not h.get("isLpHolder") and not h.get("is_lp_holder")]
            if non_lp:
                max_holder_pct = max(h.get("pct", 0) or h.get("percentage", 0) for h in non_lp)

        # 5. LP locked %
        lp_pct = 0
        markets = d.get("markets") or []
        if markets:
            lp_pct = markets[0].get("lpLockedPct") or markets[0].get("lp_locked_pct") or 0
            lp_pct = min(100, round(float(lp_pct)))
        if not lp_pct:
            lp_pct_direct = d.get("lpLockedPct") or d.get("lp_locked_pct") or 0
            lp_pct = min(100, round(float(lp_pct_direct)))

        # 6. Creator balance
        creator_sold = (
            d.get("creatorBalance") == "SOLD" or
            d.get("creator_balance") == "SOLD" or
            (isinstance(d.get("creatorTokens"), (int, float)) and d["creatorTokens"] == 0)
        )

        # 7. Overall score
        score = 0
        if isinstance(d.get("score"), (int, float)):
            score = min(100, max(0, round(d["score"])))
        elif isinstance(d.get("score_normalised"), (int, float)):
            score = min(100, max(0, round(d["score_normalised"])))

        # 8. Parse risks list for specific danger flags
        risks = d.get("risks") or []
        risk_names = [r.get("name", "").lower() for r in risks]
        risk_levels = {r.get("name", "").lower(): r.get("level", "").lower() for r in risks}

        has_freeze_risk = any("freeze" in r for r in risk_names)
        has_mint_risk = any("mint" in r for r in risk_names)
        is_honeypot = any("honeypot" in r for r in risk_names)
        high_risks = [r for r in risks if r.get("level", "").lower() == "danger"]

        # Combine freeze/mint detection
        freeze_danger = has_freeze or has_freeze_risk
        mint_danger = has_mint or has_mint_risk

        return {
            "score": score,
            "lp_pct": lp_pct,
            "creator_sold": creator_sold,
            "freeze_authority": freeze_danger,
            "mint_authority": mint_danger,
            "mutable_metadata": mutable,
            "max_holder_pct": max_holder_pct,
            "is_honeypot": is_honeypot,
            "high_risks": high_risks,
            "risk_count": len(risks),
        }

    except Exception as e:
        log.warning(f"RugCheck exception {addr[:12]}: {e}")
        return None

def safety_verdict(rc):
    """
    Returns (is_safe, verdict, pass_list, fail_list)
    ANY critical flag = instant NOGO
    """
    passes = []
    fails = []
    critical_fail = False

    # 1. Freeze authority — CRITICAL
    if rc["freeze_authority"]:
        fails.append("❌ FREEZE AUTHORITY aktiv — kan frysa dina tokens!")
        critical_fail = True
    else:
        passes.append("✓ Ingen freeze authority")

    # 2. Mint authority — CRITICAL
    if rc["mint_authority"]:
        fails.append("❌ MINT AUTHORITY aktiv — kan skapa fler tokens!")
        critical_fail = True
    else:
        passes.append("✓ Ingen mint authority")

    # 3. Honeypot — CRITICAL
    if rc["is_honeypot"]:
        fails.append("❌ HONEYPOT — kan inte sälja!")
        critical_fail = True
    else:
        passes.append("✓ Inte honeypot")

    # 4. Mutable metadata — CRITICAL
    if rc["mutable_metadata"]:
        fails.append("⚠️ Metadata kan ändras")
        critical_fail = True
    else:
        passes.append("✓ Metadata låst")

    # 5. LP locked
    if rc["lp_pct"] >= 80:
        passes.append(f"✓ LP Locked {rc['lp_pct']}%")
    elif rc["lp_pct"] >= 50:
        passes.append(f"✓ LP delvis låst {rc['lp_pct']}% — ok")
    elif rc["lp_pct"] >= 20:
        fails.append(f"⚠️ LP låst {rc['lp_pct']}% — lågt")
    else:
        fails.append(f"❌ LP EJ låst ({rc['lp_pct']}%) — rug pull risk!")
        critical_fail = True

    # 6. Top holder concentration
    if rc["max_holder_pct"] > 30:
        fails.append(f"❌ En wallet äger {rc['max_holder_pct']:.1f}% — manipulation risk!")
        critical_fail = True
    elif rc["max_holder_pct"] > 15:
        fails.append(f"⚠️ En wallet äger {rc['max_holder_pct']:.1f}% — håll koll")
    else:
        passes.append(f"✓ Ingen wallet äger för mycket")

    # 7. Creator sold
    if rc["creator_sold"]:
        passes.append("✓ Creator SOLD")
    else:
        fails.append("⚠️ Creator håller tokens")

    # 8. RugCheck score
    if rc["score"] >= 70:
        passes.append(f"✓ Score {rc['score']}/100")
    elif rc["score"] >= 50:
        passes.append(f"✓ Score {rc['score']}/100 — ok")
    elif rc["score"] >= 30:
        fails.append(f"⚠️ Score {rc['score']}/100 — lågt, kolla manuellt")
    else:
        fails.append(f"❌ Score {rc['score']}/100 — för lågt")
        critical_fail = True

    # 9. High risk count
    if len(rc["high_risks"]) > 0:
        risk_names = ", ".join([r.get("name", "?") for r in rc["high_risks"][:3]])
        fails.append(f"❌ {len(rc['high_risks'])} DANGER-risker: {risk_names}")
        critical_fail = True

    is_safe = not critical_fail
    return is_safe, passes, fails

def momentum_signal(pct5m, pct1h, buys, sells, vol1h):
    signals = []
    score = 0

    if pct5m > 5:
        signals.append(f"📈 +{pct5m:.1f}% senaste 5 min — starkt")
        score += 2
    elif pct5m > 0:
        signals.append(f"📈 +{pct5m:.1f}% senaste 5 min")
        score += 1
    elif pct5m < -10:
        signals.append(f"📉 {pct5m:.1f}% senaste 5 min — varning!")
        score -= 2
    else:
        signals.append(f"📉 {pct5m:.1f}% senaste 5 min")
        score -= 1

    ratio = buys / max(sells, 1)
    if ratio >= 2:
        signals.append(f"🟢 {buys} köp vs {sells} sälj ({ratio:.1f}x) — starkt")
        score += 2
    elif ratio >= 1.3:
        signals.append(f"🟢 {buys} köp vs {sells} sälj ({ratio:.1f}x)")
        score += 1
    elif ratio < 0.8:
        signals.append(f"🔴 {buys} köp vs {sells} sälj — säljtryck!")
        score -= 2
    else:
        signals.append(f"🟡 {buys} köp vs {sells} sälj ({ratio:.1f}x) — neutralt")

    if vol1h >= 50_000:
        signals.append(f"🔥 Vol {fmt(vol1h)} — mycket aktiv")
        score += 2
    elif vol1h >= 10_000:
        signals.append(f"✅ Vol {fmt(vol1h)} — aktiv")
        score += 1
    else:
        signals.append(f"⚠️ Vol {fmt(vol1h)} — låg")

    if score >= 4: verdict = "🔥 STARKT BULLISH — bra entry"
    elif score >= 2: verdict = "✅ BULLISH — rimlig entry"
    elif score >= 0: verdict = "🟡 NEUTRAL — vänta"
    else: verdict = "🔴 BEARISH — undvik"

    return verdict, signals

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

    # Basic filters
    if age < MIN_AGE_MIN or age > MAX_AGE_H * 60: return
    if liq < MIN_LIQ or vol1h < MIN_VOL_1H: return
    if ratio < BUY_SELL_RATIO: return

    log.info(f"🛡 RugCheck FULL: {name} age={age:.0f}min liq={fmt(liq)}")
    rc = await full_rugcheck(session, addr)
    await asyncio.sleep(0.5)

    if not rc:
        log.info(f"  → Ingen RugCheck data för {name} — skippar")
        return

    is_safe, passes, fails = safety_verdict(rc)

    log.info(f"  → safe={is_safe} score={rc['score']} lp={rc['lp_pct']}% freeze={rc['freeze_authority']} mint={rc['mint_authority']}")

    if not is_safe:
        log.info(f"  → NOGO pga säkerhetsproblem: {[f for f in fails if '❌' in f]}")
        return

    # Market verdict
    if rc["score"] >= 70 and rc["lp_pct"] >= 80:
        market_verdict = "🔥 GO"
    elif rc["score"] >= 50:
        market_verdict = "✅ GO"
    else:
        market_verdict = "⚠️ WARN — kolla manuellt på RugCheck!"

    momentum_v, momentum_s = momentum_signal(pct5m, pct1h, buys, sells, vol1h)

    msg = (
        f"{market_verdict}: *{name}* (${ticker})\n"
        f"⏱ {age:.0f} min gammal\n\n"
        f"💰 MCap: {fmt(mcap)}\n"
        f"💧 Liq: {fmt(liq)}\n"
        f"📈 Vol 1h: {fmt(vol1h)} | 24h: {fmt(vol24h)}\n"
        f"📊 Pris 1h: {'+' if pct1h>=0 else ''}{pct1h:.1f}% | 5min: {'+' if pct5m>=0 else ''}{pct5m:.1f}%\n"
        f"🔄 Buys/Sells: {buys}/{sells}\n\n"
        f"🔐 *SÄKERHETSKONTROLL:*\n"
        + "\n".join(passes + fails) +
        f"\n\n📊 *CHARTANALYS:*\n"
        f"{momentum_v}\n"
        + "\n".join(momentum_s) +
        f"\n\n🎯 Entry: Nu om momentum håller\n"
        f"🛑 Stop loss: −20%\n"
        f"💰 Target: +50–100%\n\n"
        f"🔗 [Dexscreener](https://dexscreener.com/solana/{addr})\n"
        f"🔍 [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"🟣 [Pump.fun](https://pump.fun/{addr})\n\n"
        f"⚡ _PumpScan Bot v4_"
    )

    notified.add(addr)
    await send_tg(session, msg)
    log.info(f"  → {market_verdict} skickad för {name}!")

async def dex_scan_loop(session):
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
    log.info("🚀 PumpScan Bot v4 startar...")
    conn = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        await send_tg(session,
            "🤖 *PumpScan Bot v4 startad!*\n\n"
            "🔐 *Nya säkerhetskontroller:*\n"
            "✓ Freeze authority check\n"
            "✓ Mint authority check\n"
            "✓ Honeypot check\n"
            "✓ Mutable metadata check\n"
            "✓ Top holder koncentration\n"
            "✓ LP locked check\n"
            "✓ DANGER-risker blockeras\n\n"
            "Ingen coin med kritiska risker skickas till dig!\n\n"
            "⚡ _PumpScan Bot v4_"
        )
        await dex_scan_loop(session)

if __name__ == "__main__":
    asyncio.run(main())
