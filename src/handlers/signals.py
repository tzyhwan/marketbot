"""/autosignal, /stopsignal, /signals — auto-signal scheduling."""
from __future__ import annotations

import asyncio
import io

import structlog
from telegram import InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from src.chart.generator import fetch_klines, generate_chart
from src.chart.market_sessions import get_current_sessions
from src.core.message_utils import send_long_to_chat

log = structlog.get_logger()

INTERVAL_HOURS = {
    "15M": 0.25, "30M": 0.5,
    "1H": 1, "2H": 2, "4H": 4,
    "6H": 6, "8H": 8, "12H": 12, "1D": 24,
}


def _job_name(chat_id: int, asset: str, timeframe: str) -> str:
    return f"autosignal_{chat_id}_{asset}_{timeframe}"


async def _auto_signal_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: run analysis and send chart + signal to a chat."""
    chat_id: int = ctx.job.data["chat_id"]
    asset: str = ctx.job.data["asset"]
    timeframe: str = ctx.job.data["timeframe"]
    deps = ctx.bot_data

    log.info("auto_signal_firing", asset=asset, timeframe=timeframe, chat_id=chat_id)

    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        engine = deps["trading_engine"]
        analysis_text, levels = await engine.suggest_trade(asset, timeframe)

        session_info = get_current_sessions()
        db = deps["db"]
        await db.record_signal(
            chat_id=chat_id, asset=asset, timeframe=timeframe,
            direction=levels.get("direction") if levels else None,
            entry=levels.get("entry") if levels else None,
            sl=levels.get("sl") if levels else None,
            tp1=levels.get("tp1") if levels else None,
            tp2=levels.get("tp2") if levels else None,
            tp3=levels.get("tp3") if levels else None,
            market_session=session_info["primary_session"],
            session_detail=session_info,
            analysis_text=analysis_text,
            source="autosignal",
        )

        try:
            binance = deps["binance"]
            df = await fetch_klines(binance, asset, timeframe)
            img_bytes = await asyncio.to_thread(generate_chart, df, asset, timeframe, levels)
            await ctx.bot.send_photo(
                chat_id,
                photo=InputFile(io.BytesIO(img_bytes), filename=f"{asset}_{timeframe}.png"),
            )
        except Exception as e:
            log.warning("autosignal_chart_failed", error=str(e))

        await send_long_to_chat(ctx.bot, chat_id, analysis_text)

    except Exception as e:
        log.error("autosignal_job_failed", asset=asset, timeframe=timeframe, error=str(e))


async def cmd_autosignal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    deps = ctx.bot_data
    db = deps["db"]
    args = ctx.args or []

    if not args:
        await update.message.reply_text(
            "<b>Auto Signal — Scheduled alerts</b>\n\n"
            "Usage: /autosignal &lt;asset&gt; [timeframe]\n\n"
            "Examples:\n"
            "  /autosignal BTCUSDT 4H\n"
            "  /autosignal ETHUSDT 1H\n\n"
            "Timeframes: 15M, 30M, 1H, 2H, 4H, 6H, 8H, 12H, 1D\n\n"
            "Sends a full chart + analysis at every interval.\n"
            "Use /stopsignal to stop.",
            parse_mode=ParseMode.HTML,
        )
        return

    asset = args[0].upper()
    timeframe = args[1].upper() if len(args) > 1 else "4H"
    chat_id = update.effective_chat.id

    if timeframe not in INTERVAL_HOURS:
        await update.message.reply_text(
            f"Invalid timeframe: {timeframe}\nValid: {', '.join(INTERVAL_HOURS.keys())}"
        )
        return

    job_name = _job_name(chat_id, asset, timeframe)
    existing = ctx.job_queue.get_jobs_by_name(job_name)
    if existing:
        await update.message.reply_text(
            f"Already tracking <b>{asset} {timeframe}</b>. Use /stopsignal to remove it first.",
            parse_mode=ParseMode.HTML,
        )
        return

    await db.save_autosignal_sub(chat_id, asset, timeframe)

    interval_secs = INTERVAL_HOURS[timeframe] * 3600
    ctx.job_queue.run_repeating(
        _auto_signal_job,
        interval=interval_secs,
        first=10,
        name=job_name,
        data={"chat_id": chat_id, "asset": asset, "timeframe": timeframe},
    )

    await update.message.reply_text(
        f"Auto-signal enabled: <b>{asset} {timeframe}</b>\n"
        f"You'll receive a chart + analysis every <b>{timeframe}</b>.\n"
        f"First signal coming in a few seconds...\n\n"
        f"Use /stopsignal to manage signals.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stopsignal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    deps = ctx.bot_data
    db = deps["db"]
    args = ctx.args or []
    chat_id = update.effective_chat.id

    if not args:
        # Stop all signals for this chat
        subs = await db.get_active_autosignal_subs()
        my_subs = [s for s in subs if s["chat_id"] == chat_id]
        if not my_subs:
            await update.message.reply_text("No active auto-signals for this chat.")
            return

        removed = []
        for sub in my_subs:
            name = _job_name(chat_id, sub["asset"], sub["timeframe"])
            for job in ctx.job_queue.get_jobs_by_name(name):
                job.schedule_removal()
            removed.append(f"{sub['asset']} {sub['timeframe']}")
        await db.remove_all_autosignal_subs(chat_id)
        await update.message.reply_text(
            "Stopped all auto-signals:\n" + "\n".join(f"  - {r}" for r in removed)
        )
        return

    asset = args[0].upper()
    timeframe = args[1].upper() if len(args) > 1 else "4H"
    name = _job_name(chat_id, asset, timeframe)

    jobs = ctx.job_queue.get_jobs_by_name(name)
    if not jobs:
        await update.message.reply_text(
            f"No active auto-signal for <b>{asset} {timeframe}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    for job in jobs:
        job.schedule_removal()
    await db.remove_autosignal_sub(chat_id, asset, timeframe)
    await update.message.reply_text(
        f"Stopped auto-signal: <b>{asset} {timeframe}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    deps = ctx.bot_data
    db = deps["db"]
    chat_id = update.effective_chat.id

    subs = await db.get_active_autosignal_subs()
    my_subs = [s for s in subs if s["chat_id"] == chat_id]

    if not my_subs:
        await update.message.reply_text("No active auto-signals. Use /autosignal to add one.")
        return

    lines = [f"  - <b>{s['asset']} {s['timeframe']}</b>" for s in my_subs]
    await update.message.reply_text(
        "<b>Active auto-signals:</b>\n" + "\n".join(lines) + "\n\nUse /stopsignal to stop one or all.",
        parse_mode=ParseMode.HTML,
    )


async def restore_autosignal_subs(app) -> None:
    """Restore persisted subscriptions from the database on startup."""
    db = app.bot_data["db"]
    subs = await db.get_active_autosignal_subs()
    if not subs:
        return

    for sub in subs:
        chat_id = sub["chat_id"]
        asset = sub["asset"]
        timeframe = sub["timeframe"]

        if timeframe not in INTERVAL_HOURS:
            continue

        job_name = _job_name(chat_id, asset, timeframe)
        interval_secs = INTERVAL_HOURS[timeframe] * 3600
        app.job_queue.run_repeating(
            _auto_signal_job,
            interval=interval_secs,
            first=30,
            name=job_name,
            data={"chat_id": chat_id, "asset": asset, "timeframe": timeframe},
        )
        log.info("restored_autosignal", asset=asset, timeframe=timeframe, chat_id=chat_id)

    log.info("autosignal_restore_complete", count=len(subs))
