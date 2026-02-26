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
COINGLASS_API_KEY   = os.environ.get("COINGLASS_API_KEY")

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


def get_coinglass_data():
    headers = {"coinglassSecret": COINGLASS_API_KEY}
    result = {}
    try:
        r = requests.get(
            "https://open-api.coinglass.com/public/v2/open_interest",
            headers=headers,
            params={"symbol": "BTC"},
            timeout=10,
        )
        result["open_interest"] = r.json() if r.ok else "Error {}".format(r.status_code)
    except Exception as e:
        result["open_interest"] = str(e)
    try:
        r = requests.get(
            "https://open-api.coinglass.com/public/v2/liquidation_history",
            headers=headers,
            params={"symbol": "BTC", "interval": "1d"},
            timeout=10,
        )
        result["liquidations"] = r.json() if r.ok else "Error {}".format(r.status_code)
    except Exception as e:
        result["liquidations"] = str(e)
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

def claude_market_snapshot(btc, cg):
    prompt = """You are a concise crypto market analyst writing for a Telegram bot.

Real-time BTC data:
- Price:      ${:,}
- 24h Change: {:.2f}%
- Market Cap: ${:,.0f}
- 24h Volume: ${:,.0f}

CoinGlass Open Interest: {}
CoinGlass Liquidations:  {}

Write a punchy 4-6 sentence snapshot. Highlight any notable signals.
End with a one-line bias: Bullish or Bearish or Neutral and why.
Do not use ## headers or ** bold markdown. Use plain text with emojis only.""".format(
        btc.get("price", 0),
        btc.get("change_24h", 0),
        btc.get("market_cap", 0),
        btc.get("volume_24h", 0),
        str(cg.get("open_interest", "N/A"))[:600],
        str(cg.get("liquidations", "N/A"))[:600],
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
        "/market  - Live BTC snapshot + CoinGlass\n"
        "/news    - Latest crypto news last 1hr\n"
        "/weekly  - Full catalyst report with predictions\n"
        "/fed     - Fed rate cut predictions from Polymarket\n"
        "/help    - Show this menu\n\n"
        "Auto weekly report fires every Monday at 8:00 AM UTC."
    )
    await update.message.reply_text(text)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching real-time data...")
    btc      = get_btc_price()
    cg       = get_coinglass_data()
    analysis = claude_market_snapshot(btc, cg)
    change_emoji = "UP" if btc.get("change_24h", 0) > 0 else "DOWN"
    msg = (
        "BTC Market Snapshot\n"
        "Price:      ${:,}\n"
        "24h Change: {} {:.2f}%\n"
        "Volume:     ${:.2f}B\n"
        "Mkt Cap:    ${:.1f}B\n\n"
        "AI Analysis:\n{}\n\n"
        "Time: {} UTC"
    ).format(
        btc.get("price", 0),
        change_emoji,
        btc.get("change_24h", 0),
        btc.get("volume_24h", 0) / 1e9,
        btc.get("market_cap", 0) / 1e9,
        analysis,
        datetime.now(timezone.utc).strftime("%H:%M"),
    )
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
