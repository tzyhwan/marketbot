"""
Market Intelligence Telegram Bot
- All commands use Claude Opus
- Polymarket Fed rate predictions via /fed
- Auto weekly report every Monday 8AM UTC
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
import anthropic

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIG
# ============================================================
import os
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID             = os.environ.get("CHAT_ID")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")

# ============================================================
# AI CLIENT
# ============================================================
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ============================================================
# DATA FETCHERS
# ============================================================

def get_btc_price():
    try:
        # Binance public API - no key required, very reliable
        ticker_url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
        r = requests.get(ticker_url, timeout=10)
        r.raise_for_status()
        d = r.json()
        price      = float(d["lastPrice"])
        change_24h = float(d["priceChangePercent"])
        volume_24h = float(d["quoteVolume"])  # in USD

        # Get market cap from CoinGecko as fallback (best effort)
        market_cap = 0
        try:
            cg = requests.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd&include_market_cap=true",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=6,
            )
            if cg.ok:
                market_cap = cg.json()["bitcoin"].get("usd_market_cap", 0)
        except Exception:
            market_cap = price * 19_700_000  # rough estimate from circulating supply

        return {
            "price":      price,
            "change_24h": change_24h,
            "market_cap": market_cap,
            "volume_24h": volume_24h,
        }
    except Exception as e:
        return {"error": str(e)}


def get_binance_futures_data():
    """Fetch BTC derivatives data from Binance Futures — no API key needed."""
    result = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    one_hour_ago_ms = now_ms - 3600 * 1000

    # Open Interest
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        if r.ok:
            oi = float(r.json()["openInterest"])
            pr = requests.get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=5)
            btc_price = float(pr.json()["price"]) if pr.ok else 0
            result["oi_usd"] = round(oi * btc_price / 1e9, 2)
        else:
            result["oi_usd"] = None
    except Exception:
        result["oi_usd"] = None

    # Funding Rate
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": "BTCUSDT", "limit": 1}, timeout=10)
        result["funding_rate"] = round(float(r.json()[0]["fundingRate"]) * 100, 4) if r.ok else None
    except Exception:
        result["funding_rate"] = None

    # L/S Ratio — 5 min
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                         params={"symbol": "BTCUSDT", "period": "5m", "limit": 1}, timeout=10)
        if r.ok:
            d = r.json()[0]
            result["long_pct"]  = round(float(d["longAccount"])  * 100, 1)
            result["short_pct"] = round(float(d["shortAccount"]) * 100, 1)
        else:
            result["long_pct"] = None
    except Exception:
        result["long_pct"] = None

    # Liquidations — last 1 hour
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/allForceOrders",
                         params={"symbol": "BTCUSDT", "startTime": one_hour_ago_ms, "limit": 1000}, timeout=10)
        if r.ok:
            orders = r.json()
            liq_long  = sum(float(o["origQty"]) * float(o["price"]) for o in orders if o["side"] == "SELL")
            liq_short = sum(float(o["origQty"]) * float(o["price"]) for o in orders if o["side"] == "BUY")
            result["liq_longs_usd"]  = round(liq_long  / 1e6, 2)
            result["liq_shorts_usd"] = round(liq_short / 1e6, 2)
        else:
            result["liq_longs_usd"] = None
    except Exception:
        result["liq_longs_usd"] = None

    return result


def get_bybit_futures_data():
    """Fetch BTC derivatives data from Bybit — no API key needed."""
    result = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    one_hour_ago_ms = now_ms - 3600 * 1000

    # Open Interest
    try:
        r = requests.get("https://api.bybit.com/v5/market/open-interest",
                         params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h", "limit": 1}, timeout=10)
        if r.ok:
            data = r.json()
            oi_list = data.get("result", {}).get("list", [])
            if oi_list:
                oi_usd = float(oi_list[0]["openInterestValue"])
                result["oi_usd"] = round(oi_usd / 1e9, 2)
            else:
                result["oi_usd"] = None
        else:
            result["oi_usd"] = None
    except Exception:
        result["oi_usd"] = None

    # Funding Rate
    try:
        r = requests.get("https://api.bybit.com/v5/market/funding/history",
                         params={"category": "linear", "symbol": "BTCUSDT", "limit": 1}, timeout=10)
        if r.ok:
            lst = r.json().get("result", {}).get("list", [])
            result["funding_rate"] = round(float(lst[0]["fundingRate"]) * 100, 4) if lst else None
        else:
            result["funding_rate"] = None
    except Exception:
        result["funding_rate"] = None

    # L/S Ratio — 5 min
    try:
        r = requests.get("https://api.bybit.com/v5/market/account-ratio",
                         params={"category": "linear", "symbol": "BTCUSDT", "period": "5min", "limit": 1}, timeout=10)
        if r.ok:
            lst = r.json().get("result", {}).get("list", [])
            if lst:
                buy_ratio = float(lst[0]["buyRatio"])
                result["long_pct"]  = round(buy_ratio * 100, 1)
                result["short_pct"] = round((1 - buy_ratio) * 100, 1)
            else:
                result["long_pct"] = None
        else:
            result["long_pct"] = None
    except Exception:
        result["long_pct"] = None

    # Liquidations — last 1 hour (Bybit provides liq records)
    try:
        r = requests.get("https://api.bybit.com/v5/market/recent-trade",
                         params={"category": "linear", "symbol": "BTCUSDT", "limit": 1000}, timeout=10)
        # Bybit doesn't have a free liq endpoint; mark N/A
        result["liq_longs_usd"]  = None
        result["liq_shorts_usd"] = None
    except Exception:
        result["liq_longs_usd"]  = None
        result["liq_shorts_usd"] = None

    # Use liquidation websocket data via REST snapshot
    try:
        r = requests.get("https://api.bybit.com/v5/market/liquidation",
                         params={"category": "linear", "symbol": "BTCUSDT", "limit": 200}, timeout=10)
        if r.ok:
            lst = r.json().get("result", {}).get("list", [])
            liq_long  = sum(float(o["size"]) * float(o["price"]) for o in lst if o["side"] == "Buy")
            liq_short = sum(float(o["size"]) * float(o["price"]) for o in lst if o["side"] == "Sell")
            result["liq_longs_usd"]  = round(liq_long  / 1e6, 2)
            result["liq_shorts_usd"] = round(liq_short / 1e6, 2)
    except Exception:
        pass

    return result


def get_hyperliquid_data():
    """Fetch BTC derivatives data from Hyperliquid — no API key needed."""
    result = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    one_hour_ago_ms = now_ms - 3600 * 1000

    try:
        # Meta + asset contexts gives OI and funding
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        if r.ok:
            data = r.json()
            universe = data[0].get("universe", [])
            ctxs     = data[1]
            btc_idx  = next((i for i, a in enumerate(universe) if a["name"] == "BTC"), None)
            if btc_idx is not None:
                ctx = ctxs[btc_idx]
                oi_usd = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0))
                result["oi_usd"]       = round(oi_usd / 1e9, 2)
                result["funding_rate"] = round(float(ctx.get("funding", 0)) * 100, 4)
            else:
                result["oi_usd"] = None
        else:
            result["oi_usd"] = None
    except Exception:
        result["oi_usd"] = None

    # L/S Ratio via globalSummary
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "assetPositions", "user": "0x0000000000000000000000000000000000000000"},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        result["long_pct"]  = None
        result["short_pct"] = None
    except Exception:
        result["long_pct"]  = None
        result["short_pct"] = None

    # Use globalFundingHistory for L/S approximation via openInterest sides
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "fundingHistory", "coin": "BTC",
                                "startTime": one_hour_ago_ms},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        # fundingHistory doesn't give L/S directly; skip
    except Exception:
        pass

    # Liquidations — last 1 hour
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "userFills", "user": "0x0000000000000000000000000000000000000000"},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        result["liq_longs_usd"]  = None
        result["liq_shorts_usd"] = None
    except Exception:
        result["liq_longs_usd"]  = None
        result["liq_shorts_usd"] = None

    # Proper liquidation endpoint
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "liquidations", "coin": "BTC",
                                "startTime": one_hour_ago_ms, "endTime": now_ms},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        if r.ok and isinstance(r.json(), list):
            liqs = r.json()
            liq_long  = sum(float(l.get("sz", 0)) * float(l.get("px", 0)) for l in liqs if l.get("side") == "B")
            liq_short = sum(float(l.get("sz", 0)) * float(l.get("px", 0)) for l in liqs if l.get("side") == "A")
            result["liq_longs_usd"]  = round(liq_long  / 1e6, 2)
            result["liq_shorts_usd"] = round(liq_short / 1e6, 2)
    except Exception:
        pass

    return result

def get_crypto_news(limit=12, hours=6):
    try:
        r = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        results = []
        for item in root.findall(".//item"):
            title   = item.findtext("title", "")
            url     = item.findtext("link", "")
            pub_raw = item.findtext("pubDate", "")
            try:
                pub_time = parsedate_to_datetime(pub_raw)
                if pub_time < cutoff:
                    continue
            except Exception:
                pass
            results.append({"title": title, "url": url, "source": "CoinDesk"})
            if len(results) >= limit:
                break
        return results  # Empty list = genuinely no news
    except Exception as e:
        return [{"title": "News fetch error: {}".format(e), "url": "", "source": ""}]


def get_polymarket_fed_data():
    markets = [
        {"slug": "fed-decision-in-march-885", "label": "Mar 18, 2026"},
        {"slug": "fed-decision-in-april",     "label": "Apr 29, 2026"},
        {"slug": "fed-decision-in-june-825",  "label": "Jun 17, 2026"},
    ]
    results = []
    for m in markets:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/events/slug/{}".format(m["slug"]),
                timeout=10,
            )
            if not r.ok:
                results.append({"label": m["label"], "outcomes": {}})
                continue
            data = r.json()
            # Each market in the event is one outcome (No change, 25bps cut, etc)
            outcomes = {}
            for market in data.get("markets", []):
                question = market.get("question", "")
                prices   = market.get("outcomePrices", "[]")
                out_list = market.get("outcomes", '["Yes","No"]')
                try:
                    prices_list = json.loads(prices) if isinstance(prices, str) else prices
                    out_list    = json.loads(out_list) if isinstance(out_list, str) else out_list
                    yes_price   = float(prices_list[0]) * 100
                except Exception:
                    yes_price = 0
                # Clean up question to get just the outcome name
                q = question.lower()
                if "no change" in q or "hold" in q or "unchanged" in q:
                    label = "No Change"
                elif "50+" in q or "50 +" in q or "50 basis" in q:
                    label = "50+ bps Cut"
                elif ("25" in q and ("cut" in q or "decrease" in q or "lower" in q or "reduction" in q or "basis" in q)) and "50" not in q:
                    label = "25 bps Cut"
                elif "increase" in q or "hike" in q or "raise" in q:
                    label = "25+ bps Hike"
                else:
                    label = question[:40]  # Keep full label for debugging
                outcomes[label] = round(yes_price, 1)
            results.append({"label": m["label"], "outcomes": outcomes})
        except Exception as e:
            results.append({"label": m["label"], "outcomes": {}})
    return results


# ============================================================
# AI ANALYSIS
# ============================================================

def claude_market_snapshot(btc, bin_f, byb_f, hl_f):
    def fmt_oi(d):   return "${:.2f}B".format(d["oi_usd"]) if d.get("oi_usd") is not None else "N/A"
    def fmt_fr(d):   return "{:.4f}%".format(d["funding_rate"]) if d.get("funding_rate") is not None else "N/A"
    def fmt_ls(d):   return "L {:.1f}% / S {:.1f}%".format(d["long_pct"], d["short_pct"]) if d.get("long_pct") is not None else "N/A"
    def fmt_liq(d):  return "Longs ${:.2f}M / Shorts ${:.2f}M".format(d["liq_longs_usd"], d["liq_shorts_usd"]) if d.get("liq_longs_usd") is not None else "N/A"

    prompt = """You are a concise crypto market analyst writing for a Telegram bot.

BTC Price: ${:,} ({:.2f}% 24h)  |  Volume: ${:,.0f} (Binance)

Derivatives snapshot across exchanges (5min L/S | 1hr liquidations):

Binance Futures:
- OI: {}  Funding: {}  L/S (5m): {}  Liqs (1h): {}

Bybit:
- OI: {}  Funding: {}  L/S (5m): {}  Liqs (1h): {}

Hyperliquid:
- OI: {}  Funding: {}  Liqs (1h): {}

Write a punchy 4-6 sentence snapshot. Compare signals across exchanges where interesting. Highlight funding extremes, OI divergence, or liq imbalances.
End with a one-line bias: Bullish / Bearish / Neutral and why.
Do not use ## headers or ** bold. Plain text + emojis only.""".format(
        btc.get("price", 0), btc.get("change_24h", 0), btc.get("volume_24h", 0),
        fmt_oi(bin_f), fmt_fr(bin_f), fmt_ls(bin_f), fmt_liq(bin_f),
        fmt_oi(byb_f), fmt_fr(byb_f), fmt_ls(byb_f), fmt_liq(byb_f),
        fmt_oi(hl_f),  fmt_fr(hl_f),  fmt_liq(hl_f),
    )
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def claude_news_summary(news_items):
    headlines = "\n".join("- [{}] {}".format(n["source"], n["title"]) for n in news_items)
    prompt = """You are a crypto/macro news analyst. Summarise these headlines for a trader.
Group them into: Macro Events | BTC/Crypto | Regulation.
For each group, pick the 2-3 most market-moving stories and give a one-sentence impact note.
Keep it tight for Telegram.
Do not use ## headers or ** bold markdown. Use plain text with emojis only.

Headlines:
{}""".format(headlines)
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def claude_weekly_report(news_items, btc):
    headlines = "\n".join("- [{}] {}".format(n["source"], n["title"]) for n in news_items)
    prompt = """You are a senior macro and crypto strategist producing a weekly pre-market briefing.

Current BTC price: ${:,} ({:.2f}% 24h)
Report date: {}

Recent news headlines:
{}

Produce a structured weekly catalyst report covering:
1. KEY CATALYSTS THIS WEEK - list 4-6 upcoming events with date, prediction (Bullish/Bearish/Neutral) and 2-sentence reasoning
2. OVERALL WEEKLY BIAS - Bullish/Bearish/Neutral with reasoning
3. KEY RISKS TO WATCH - 2-3 tail risks
4. LEVELS TO WATCH - key BTC support/resistance

Do not use ## headers or ** bold markdown. Use plain text with emojis only.""".format(
        btc.get("price", 0),
        btc.get("change_24h", 0),
        datetime.now().strftime("%A %B %d, %Y"),
        headlines,
    )
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def claude_fed_analysis(summary):
    prompt = """You are a macro analyst. Based on these Polymarket prediction market probabilities for Fed rate decisions, give a concise analysis:

{}

Cover:
1. What the market is currently pricing in for each meeting
2. Impact on BTC and risk assets if cuts happen vs stay on hold
3. One-line trading bias

Do not use ## headers or ** bold markdown. Use plain text with emojis only.
Keep it tight for Telegram.""".format(summary)
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Market Intelligence Bot\n\n"
        "Commands:\n"
        "/market  - Live BTC snapshot + Binance Futures\n"
        "/news    - Latest crypto news last 1hr\n"
        "/weekly  - Full catalyst report with predictions\n"
        "/fed     - Fed rate cut predictions from Polymarket\n"
        "/help    - Show this menu\n\n"
        "Auto weekly report fires every Monday at 8:00 AM UTC."
    )
    await update.message.reply_text(text)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching real-time data from Binance, Bybit, Hyperliquid...")
    btc   = get_btc_price()
    bin_f = get_binance_futures_data()
    byb_f = get_bybit_futures_data()
    hl_f  = get_hyperliquid_data()
    analysis = claude_market_snapshot(btc, bin_f, byb_f, hl_f)

    change_emoji = "📈" if btc.get("change_24h", 0) > 0 else "📉"

    def fmt_oi(d):
        return "${:.2f}B".format(d["oi_usd"]) if d.get("oi_usd") is not None else "N/A"

    def fmt_fr(d):
        if d.get("funding_rate") is None:
            return "N/A"
        e = "🟢" if d["funding_rate"] >= 0 else "🔴"
        return "{} {:.4f}%".format(e, d["funding_rate"])

    def fmt_ls(d):
        if d.get("long_pct") is None:
            return "N/A"
        return "🟢 {:.1f}% / 🔴 {:.1f}%".format(d["long_pct"], d["short_pct"])

    def fmt_liq(d):
        if d.get("liq_longs_usd") is None:
            return "N/A"
        return "💀L ${:.2f}M  💀S ${:.2f}M".format(d["liq_longs_usd"], d["liq_shorts_usd"])

    msg = (
        "BTC Market Snapshot\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Price:      ${:,}\n"
        "24h Change: {} {:.2f}%\n"
        "Volume:     ${:.2f}B (Binance 24h)\n\n"
        "📊 Binance Futures\n"
        "OI: {}  |  Funding: {}\n"
        "L/S (5m): {}\n"
        "Liqs (1h): {}\n\n"
        "📊 Bybit\n"
        "OI: {}  |  Funding: {}\n"
        "L/S (5m): {}\n"
        "Liqs (1h): {}\n\n"
        "📊 Hyperliquid\n"
        "OI: {}  |  Funding: {}\n"
        "Liqs (1h): {}\n\n"
        "🤖 AI Analysis:\n{}\n\n"
        "Time: {} UTC"
    ).format(
        btc.get("price", 0),
        change_emoji,
        btc.get("change_24h", 0),
        btc.get("volume_24h", 0) / 1e9,
        fmt_oi(bin_f), fmt_fr(bin_f), fmt_ls(bin_f), fmt_liq(bin_f),
        fmt_oi(byb_f), fmt_fr(byb_f), fmt_ls(byb_f), fmt_liq(byb_f),
        fmt_oi(hl_f),  fmt_fr(hl_f),  fmt_liq(hl_f),
        analysis,
        datetime.now(timezone.utc).strftime("%H:%M"),
    )
    if len(msg) > 4000:
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(msg)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching last 6hr news...")
    news = get_crypto_news(limit=12, hours=6)

    if not news or (len(news) == 1 and "error" in news[0]["title"].lower()):
        await update.message.reply_text(
            "📭 No news articles found in the last 6 hours.\n\nTime: {} UTC".format(
                datetime.now(timezone.utc).strftime("%H:%M")
            )
        )
        return

    summary = claude_news_summary(news)
    links = "\n".join(
        "- {}{}  {}".format(
            n["title"][:60],
            "..." if len(n["title"]) > 60 else "",
            n["url"]
        )
        for n in news[:6] if n["url"]
    )
    msg = (
        "News Summary (Last 6 Hours)\n\n"
        "{}\n\n"
        "Top Links:\n{}\n\n"
        "Time: {} UTC"
    ).format(summary, links, datetime.now(timezone.utc).strftime("%H:%M"))
    await update.message.reply_text(msg)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generating deep catalyst report (may take ~20s)...")
    news   = get_crypto_news(limit=15)
    btc    = get_btc_price()
    report = claude_weekly_report(news, btc)
    msg = (
        "Weekly Catalyst Report\n\n"
        "{}\n\n"
        "Time: {}"
    ).format(report, datetime.now().strftime("%A %b %d, %Y - %H:%M"))
    if len(msg) > 4000:
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(msg)


async def cmd_fed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching Fed rate predictions from Polymarket...")
    fed_data = get_polymarket_fed_data()

    lines = ""
    for d in fed_data:
        lines += "\nFOMC {} \n".format(d["label"])
        if d["outcomes"]:
            # Sort: No Change first, then cuts, then hike
            order = ["No Change", "25 bps Cut", "50+ bps Cut", "25+ bps Hike"]
            shown = set()
            for key in order:
                if key in d["outcomes"]:
                    pct = d["outcomes"][key]
                    filled = int(pct / 10)
                    bar = "█" * filled + "░" * (10 - filled)
                    if key == "No Change":
                        color = "🟢"
                    elif "Cut" in key:
                        color = "🔴"
                    else:
                        color = "🟡"
                    lines += "  {} {}: {} {}%\n".format(color, key, bar, pct)
                    shown.add(key)
            # Show any unrecognised outcomes (helps debug label mismatches)
            for key, pct in d["outcomes"].items():
                if key not in shown:
                    lines += "  ❓ {}: {}%\n".format(key, pct)
        else:
            lines += "  Data unavailable\n"

    # Build summary for AI
    summary = ""
    for d in fed_data:
        if d["outcomes"]:
            summary += "\n{}: {}\n".format(d["label"], d["outcomes"])

    analysis = claude_fed_analysis(summary)

    msg = (
        "Fed Rate Predictions (Polymarket)\n"
        "==================\n"
        "{}\n"
        "AI Analysis:\n{}\n\n"
        "Time: {} UTC"
    ).format(lines, analysis, datetime.now(timezone.utc).strftime("%H:%M"))

    if len(msg) > 4000:
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(msg)


# ============================================================
# SCHEDULED WEEKLY REPORT
# ============================================================

async def run_weekly_report(app):
    try:
        news   = get_crypto_news(limit=15)
        btc    = get_btc_price()
        report = claude_weekly_report(news, btc)
        msg = (
            "AUTO Weekly Catalyst Report\n\n"
            "{}\n\n"
            "Time: {}"
        ).format(report, datetime.now().strftime("%A %b %d, %Y - %H:%M"))
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            await app.bot.send_message(chat_id=CHAT_ID, text=chunk)
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text="Weekly report failed: {}".format(e))


async def schedule_weekly(app):
    fired_today = False
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0 and now.hour == 8 and now.minute == 0 and not fired_today:
            await run_weekly_report(app)
            fired_today = True
        elif now.weekday() != 0 or now.hour != 8:
            fired_today = False
        await asyncio.sleep(30)


# ============================================================
# MAIN
# ============================================================

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("news",   cmd_news))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("fed",    cmd_fed))

    print("Market Intelligence Bot is running...")
    print("Commands: /market /news /weekly /fed")

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await schedule_weekly(app)


if __name__ == "__main__":
    asyncio.run(main())
