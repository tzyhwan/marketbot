"""TradingView webhook receiver — aiohttp server.

Ported from telegram-pinescript-bot webhook_server.py.
"""
from __future__ import annotations

import hmac
import json

import structlog
from aiohttp import web
from telegram.constants import ParseMode

log = structlog.get_logger()

# Module-level state, set once via init()
_bot = None
_bot_data: dict | None = None
_webhook_secret: str = ""


def init(secret: str, bot, bot_data: dict) -> None:
    """Wire up the webhook server with bot instance and shared state."""
    global _bot, _bot_data, _webhook_secret
    _bot = bot
    _bot_data = bot_data
    _webhook_secret = secret


def _format_alert(data: dict) -> str:
    action = str(data.get("action", "SIGNAL")).upper()
    ticker = str(data.get("ticker", "???"))
    price = str(data.get("price", "—"))
    tp = str(data.get("tp", "—"))
    sl = str(data.get("sl", "—"))
    extra = {k: v for k, v in data.items() if k not in ("action", "ticker", "price", "tp", "sl")}

    arrow = "\u2B06" if action == "LONG" else "\u2B07" if action == "SHORT" else "\u26A1"
    lines = [
        f"{arrow} <b>{action} {ticker}</b>",
        f"Price: <code>{price}</code>",
        f"TP: <code>{tp}</code>",
        f"SL: <code>{sl}</code>",
    ]
    if extra:
        lines.append(f"Details: <code>{json.dumps(extra)}</code>")
    return "\n".join(lines)


async def handle_webhook(request: web.Request) -> web.Response:
    """POST /webhook — receives TradingView alert JSON."""
    secret = request.headers.get("X-Webhook-Secret", "")
    if not hmac.compare_digest(secret, _webhook_secret):
        return web.Response(status=403, text="Forbidden")

    try:
        body = await request.text()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"action": "ALERT", "message": body}
    except Exception:
        return web.Response(status=400, text="Bad request")

    msg = _format_alert(data)
    log.info("webhook_alert_received", msg=msg)

    subscribers = _bot_data.get("alert_subscribers", set()) if _bot_data else set()

    if _bot and subscribers:
        for chat_id in subscribers:
            try:
                await _bot.send_message(
                    chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.error("webhook_send_failed", chat_id=chat_id, error=str(e))

    return web.Response(text="OK")


def create_webhook_app() -> web.Application:
    """Create the aiohttp web app for the webhook server."""
    app = web.Application(client_max_size=1024 * 1024)  # 1MB max payload
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", lambda _: web.Response(text="OK"))
    return app
