import asyncio
import re
import os
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message

TG_TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
DEEPNETS_API_KEY = os.environ["DEEPNETS_API_KEY"]
DEEPNETS_API = "https://api.deepnets.ai/api/token-safety/{}"

DEXSCREENER_BOOSTS = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{}"

PERFORMERS_INTERVAL = 600  # every 10 minutes

SOLANA_CA_RE = re.compile(r"(?:^|\s)ca[:\s]+([1-9A-HJ-NP-Za-km-z]{32,44})(?:\s|$)", re.IGNORECASE)

bot = Bot(token=TG_TOKEN)
router = Router()


# ── Deepnets helper ─────────────────────────────────────────────────────────

async def fetch_deepnets_safety(session: aiohttp.ClientSession, ca: str) -> dict | None:
    try:
        async with session.get(
            DEEPNETS_API.format(ca),
            headers={"x-api-key": DEEPNETS_API_KEY},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception as e:
        print(f"[{datetime.now()}] deepnets error: {e}", flush=True)
        return None


async def fetch_dex_data(session: aiohttp.ClientSession, ca: str) -> dict | None:
    try:
        async with session.get(DEXSCREENER_TOKEN.format(ca), timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json(content_type=None)
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            return None
        best = sorted(pairs, key=lambda p: float(p.get("volume", {}).get("h24") or 0), reverse=True)[0]
        age_ms = best.get("pairCreatedAt")
        age_hours = round((datetime.now().timestamp() * 1000 - age_ms) / 3_600_000, 1) if age_ms else None
        return {
            "name": best["baseToken"].get("name", "Unknown"),
            "symbol": best["baseToken"].get("symbol", "???"),
            "price_usd": best.get("priceUsd", "?"),
            "market_cap": float(best.get("marketCap") or best.get("fdv") or 0),
            "liquidity": float(best.get("liquidity", {}).get("usd") or 0),
            "change_24h": best.get("priceChange", {}).get("h24"),
            "volume_24h": float(best.get("volume", {}).get("h24") or 0),
            "age_hours": age_hours,
            "url": best.get("url", f"https://dexscreener.com/solana/{ca}"),
        }
    except Exception as e:
        print(f"[{datetime.now()}] dex fetch error: {e}", flush=True)
        return None


# ── CA scan command ─────────────────────────────────────────────────────────

@router.message(F.text.regexp(r"(?i)(?:^|\s)ca[\s:]+[1-9A-HJ-NP-Za-km-z]{32,44}"))
async def handle_ca_scan(message: Message):
    text = message.text or ""
    match = SOLANA_CA_RE.search(text)
    if not match:
        return

    ca = match.group(1)
    await message.reply(f"🔍 Scanning `{ca}` on Deepnets...", parse_mode="Markdown")

    async with aiohttp.ClientSession() as session:
        safety, dex = await asyncio.gather(
            fetch_deepnets_safety(session, ca),
            fetch_dex_data(session, ca),
        )

    if safety is None:
        await message.reply(f"⚠️ Could not fetch Deepnets data for `{ca}`.\nTry: https://deepnets.ai/token/{ca}", parse_mode="Markdown")
        return

    safety_level = safety.get("overallSafetyLevel", "UNKNOWN")
    name = safety.get("tokenName") or (dex["name"] if dex else "Unknown")
    symbol = safety.get("tokenSymbol") or (dex["symbol"] if dex else "???")

    if safety_level in ("SAFE", "OK"):
        verdict = "✅ *NO MAJOR RISKS*"
    elif safety_level == "RISKY":
        verdict = "🟡 *RISKY*"
    elif safety_level == "DANGEROUS":
        verdict = "🔴 *DANGEROUS*"
    else:
        verdict = f"⚠️ *{safety_level}*"

    critical_risks = safety.get("criticalRisks") or []
    warnings = safety.get("warnings") or []

    lines = [
        f"🔎 *{name}* (`{symbol}`)",
        f"CA: `{ca}`",
        "",
        f"Deepnets Verdict: {verdict}",
    ]

    if critical_risks:
        lines.append("")
        lines.append("🚨 *Critical Risks:*")
        for r in critical_risks:
            lines.append(f"  • {r}")

    if warnings:
        lines.append("")
        lines.append("⚠️ *Warnings:*")
        for w in warnings:
            lines.append(f"  • {w}")

    # Key holder stats
    top_holder = safety.get("topHolderOwnership")
    top10 = safety.get("topTenOwnership")
    network_pct = safety.get("topNetworkOwnership")
    network_wallets = safety.get("topNetworkWalletCount")
    liq_analysis = safety.get("liquidityAnalysis")
    mintable = safety.get("isMintable")
    freezable = safety.get("isFreezable")
    mutable = safety.get("isMetadataMutable")

    lines.append("")
    lines.append("*Holder Analysis:*")
    if top_holder is not None:
        lines.append(f"  Top holder: `{top_holder:.1f}%`")
    if top10 is not None:
        lines.append(f"  Top 10 holders: `{top10:.1f}%`")
    if network_pct is not None and network_wallets is not None:
        lines.append(f"  Network wallets: `{network_pct:.1f}%` across `{network_wallets}` wallets")

    lines.append("")
    lines.append("*Token Properties:*")
    lines.append(f"  Mintable: {'⚠️ Yes' if mintable else '✅ No'} | Freezable: {'⚠️ Yes' if freezable else '✅ No'} | Mutable metadata: {'⚠️ Yes' if mutable else '✅ No'}")

    if liq_analysis:
        lines.append(f"  {liq_analysis}")

    if dex:
        mc = f"${dex['market_cap']:,.0f}" if dex["market_cap"] else "N/A"
        liq = f"${dex['liquidity']:,.0f}"
        vol = f"${dex['volume_24h']:,.0f}"
        chg = f"{dex['change_24h']:+.1f}%" if dex["change_24h"] is not None else "N/A"
        lines += [
            "",
            "*Market Data:*",
            f"  Price: ${dex['price_usd']} | MC: {mc}",
            f"  Liq: {liq} | Vol 24h: {vol} | 24h: {chg}",
            f"  [View Chart]({dex['url']}) | [Deepnets](https://deepnets.ai/token/{ca})",
        ]
    else:
        lines.append(f"\n[View on Deepnets](https://deepnets.ai/token/{ca})")

    await message.reply("\n".join(lines), parse_mode="Markdown")
    print(f"[{datetime.now()}] CA scan: {ca} → {safety_level}", flush=True)


# ── Performers loop ─────────────────────────────────────────────────────────

async def fetch_top_performers(session: aiohttp.ClientSession) -> list:
    try:
        async with session.get(DEXSCREENER_BOOSTS, timeout=aiohttp.ClientTimeout(total=10)) as r:
            boosts = await r.json(content_type=None)
    except Exception as e:
        print(f"[{datetime.now()}] Failed to fetch boosts: {e}", flush=True)
        return []

    solana_tokens = [
        b["tokenAddress"]
        for b in boosts
        if isinstance(b, dict) and b.get("chainId") == "solana" and b.get("tokenAddress")
    ][:20]

    performers = []
    for ca in solana_tokens:
        try:
            async with session.get(DEXSCREENER_TOKEN.format(ca), timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json(content_type=None)
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                continue
            best = sorted(pairs, key=lambda p: float(p.get("volume", {}).get("h24") or 0), reverse=True)[0]
            change_24h = best.get("priceChange", {}).get("h24")
            if change_24h is None:
                continue
            performers.append({
                "name": best["baseToken"].get("name", "Unknown"),
                "symbol": best["baseToken"].get("symbol", "???"),
                "ca": ca,
                "change_24h": float(change_24h),
                "volume_24h": float(best.get("volume", {}).get("h24") or 0),
                "liquidity": float(best.get("liquidity", {}).get("usd") or 0),
                "price_usd": best.get("priceUsd", "?"),
                "url": best.get("url", f"https://dexscreener.com/solana/{ca}"),
            })
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[{datetime.now()}] Error fetching {ca}: {e}", flush=True)

    performers.sort(key=lambda x: x["change_24h"], reverse=True)
    return performers[:10]


async def send_performers_alert(performers: list):
    if not performers:
        await bot.send_message(chat_id=CHAT_ID, text="📊 *TOP SOLANA PERFORMERS*\n\nNo data available right now.", parse_mode="Markdown")
        return

    lines = ["📊 *TOP SOLANA PERFORMERS — 24H*\n"]
    for i, p in enumerate(performers, 1):
        change = p["change_24h"]
        arrow = "🟢" if change >= 0 else "🔴"
        vol = f"${p['volume_24h']:,.0f}"
        liq = f"${p['liquidity']:,.0f}"
        lines.append(
            f"{i}. *{p['name']}* (`{p['symbol']}`)\n"
            f"   {arrow} `{change:+.1f}%` | Vol: {vol} | Liq: {liq}\n"
            f"   CA: `{p['ca']}`\n"
            f"   [Chart]({p['url']})"
        )

    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")
    print(f"[{datetime.now()}] Sent performers alert ({len(performers)} tokens)", flush=True)


async def performers_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print(f"[{datetime.now()}] Fetching top performers...", flush=True)
                performers = await fetch_top_performers(session)
                await send_performers_alert(performers)
            except Exception as e:
                print(f"[{datetime.now()}] Performers loop error: {e}", flush=True)
            await asyncio.sleep(PERFORMERS_INTERVAL)


# ── Entry point ─────────────────────────────────────────────────────────────

async def main():
    print(f"[{datetime.now()}] Bot starting — performers every {PERFORMERS_INTERVAL}s, CA scan active", flush=True)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(performers_loop())
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
