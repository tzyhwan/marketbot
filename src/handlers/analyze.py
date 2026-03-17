"""/analyze — Full chart + AI trade analysis (Claude Sonnet)."""
from __future__ import annotations

import asyncio
import io

from telegram import InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from src.chart.generator import fetch_klines, generate_chart
from src.chart.market_sessions import get_current_sessions
from src.core.message_utils import send_long


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    deps = ctx.bot_data
    engine = deps["trading_engine"]
    db = deps["db"]
    settings = deps["settings"]

    chat_id = update.effective_chat.id
    calls = await db.get_user_calls_last_hour(chat_id)
    if calls >= settings.claude_calls_per_user_per_hour:
        await update.message.reply_text(
            "Rate limit reached. Please wait before using AI-powered commands."
        )
        return

    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /analyze &lt;asset&gt; [timeframe] [extra notes]\n"
            "Example: /analyze BTCUSDT 4H looking for swing long\n"
            "Timeframes: 15M, 30M, 1H, 4H, 1D",
            parse_mode=ParseMode.HTML,
        )
        return

    asset = args[0].upper()
    timeframe = args[1].upper() if len(args) > 1 else "1H"
    extra = " ".join(args[2:]) if len(args) > 2 else ""

    await update.message.chat.send_action(ChatAction.TYPING)

    await db.record_user_call(chat_id)
    analysis_text, levels = await engine.suggest_trade(asset, timeframe, extra)

    # Store in per-chat context for follow-up freeform messages
    chat_context = ctx.bot_data.setdefault("_chat_context", {})
    chat_context[chat_id] = analysis_text

    # Record signal for self-learning
    session_info = get_current_sessions()
    await db.record_signal(
        chat_id=chat_id,
        asset=asset,
        timeframe=timeframe,
        direction=levels.get("direction") if levels else None,
        entry=levels.get("entry") if levels else None,
        sl=levels.get("sl") if levels else None,
        tp1=levels.get("tp1") if levels else None,
        tp2=levels.get("tp2") if levels else None,
        tp3=levels.get("tp3") if levels else None,
        market_session=session_info["primary_session"],
        session_detail=session_info,
        analysis_text=analysis_text,
        source="manual",
    )

    # Generate and send chart
    try:
        binance = deps["binance"]
        df = await fetch_klines(binance, asset, timeframe)
        img_bytes = await asyncio.to_thread(generate_chart, df, asset, timeframe, levels)
        await update.message.reply_photo(
            photo=InputFile(io.BytesIO(img_bytes), filename=f"{asset}_{timeframe}.png"),
        )
    except Exception:
        pass  # Chart failure is non-fatal

    await send_long(update, analysis_text)
