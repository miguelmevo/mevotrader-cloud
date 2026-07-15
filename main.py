"""
MevoTrader — Servidor en la nube
- Telethon escucha canales Telegram
- Bot manda confirmación al móvil (botones Aprobar/Rechazar)
- FastAPI sirve endpoints para el EA en MT5 y el dashboard web
"""
import asyncio
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from telethon import TelegramClient, events
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from parser import Signal, SignalType, parse_message
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

CONFIG_PATH = Path(__file__).parent / "config.json"
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"
CHANNELS_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).parent)) / "channels.json"

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        ch = cfg["telegram"]["source_channel"]
        if not isinstance(ch, list):
            cfg["telegram"]["source_channel"] = [ch]
        return cfg

    log.info("config.json no encontrado — usando variables de entorno")
    return {
        "telegram": {
            "api_id":         os.environ["TG_API_ID"],
            "api_hash":       os.environ["TG_API_HASH"],
            "source_channel": [int(x.strip()) for x in os.environ["TG_SOURCE_CHANNEL"].split(",")],
            "bot_token":      os.environ["TG_BOT_TOKEN"],
            "admin_chat_id":  os.environ["TG_ADMIN_CHAT_ID"],
        },
        "mt5": {
            "symbol_suffix": os.environ.get("MT5_SYMBOL_SUFFIX", ""),
            "magic_number":  int(os.environ.get("MT5_MAGIC", "20240101")),
        },
        "ea": {
            "secret": os.environ["EA_SECRET"],
        },
        "trading": {
            "confirmation_timeout_seconds": int(os.environ.get("CONFIRM_TIMEOUT", "60")),
            "close_requires_confirmation":  os.environ.get("CLOSE_CONFIRM", "false").lower() == "true",
        },
    }

# -------------------------------------------------------------------
# Estado global
# -------------------------------------------------------------------
pending_for_user: dict[str, dict] = {}
pending_for_ea:   Optional[dict]  = None
last_signal:      Optional[dict]  = None
signal_history:   deque           = deque(maxlen=20)
signals_today:    int             = 0
signals_executed: int             = 0
telethon_connected: bool          = False
bot_running:      bool            = False
activity_log: deque               = deque(maxlen=100)
confirm_required: bool            = True   # False = auto-aprobar sin confirmación

# Canales dinámicos: {id: {"id": int, "name": str, "source": "env"|"dynamic"}}
extra_channels: dict[int, dict]   = {}

def _load_extra_channels():
    if CHANNELS_PATH.exists():
        try:
            data = json.loads(CHANNELS_PATH.read_text())
            return {int(k): v for k, v in data.items()}
        except Exception:
            pass
    return {}

def _save_extra_channels():
    try:
        CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CHANNELS_PATH.write_text(json.dumps(extra_channels))
    except Exception as e:
        log.warning("No se pudo guardar channels.json: %s", e)

cfg = load_config()
extra_channels = _load_extra_channels()
_telethon_client: Optional[TelegramClient] = None

def _add_log(msg: str):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    activity_log.append(f"{ts} {msg}")

# -------------------------------------------------------------------
# FastAPI
# -------------------------------------------------------------------
app = FastAPI(title="MevoTrader Cloud")

def check_secret(secret: str):
    if secret != cfg.get("ea", {}).get("secret", ""):
        raise HTTPException(status_code=403, detail="Forbidden")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    if DASHBOARD_PATH.exists():
        return HTMLResponse(DASHBOARD_PATH.read_text())
    return HTMLResponse("<h1>MevoTrader</h1><p>dashboard.html not found</p>")

# --- EA endpoints ---

@app.get("/signal/pending")
async def get_pending_signal(secret: str = Query(...)):
    check_secret(secret)
    if pending_for_ea is None:
        return None
    return pending_for_ea

@app.post("/signal/ack/{signal_id}")
async def ack_signal(signal_id: str, secret: str = Query(...)):
    check_secret(secret)
    global pending_for_ea
    if pending_for_ea and pending_for_ea.get("id") == signal_id:
        log.info("EA confirmó señal %s", signal_id)
        _add_log(f"EA ejecutó señal {signal_id}")
        pending_for_ea = None
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}

# --- Dashboard API ---

@app.get("/api/status")
async def api_status(secret: str = Query(...)):
    check_secret(secret)
    env_channels = cfg["telegram"]["source_channel"]
    total = len(env_channels) + len(extra_channels)
    return {
        "telethon_connected": telethon_connected,
        "bot_running":        bot_running,
        "pending_for_ea":     pending_for_ea,
        "last_signal":        last_signal,
        "signal_history":     list(reversed(list(signal_history))),
        "signals_today":      signals_today,
        "signals_executed":   signals_executed,
        "channels_count":     total,
        "confirm_required":   confirm_required,
    }

@app.get("/api/channels")
async def api_channels(secret: str = Query(...)):
    check_secret(secret)
    env_channels = cfg["telegram"]["source_channel"]
    result = [{"id": ch, "name": f"Canal {ch}", "source": "env"} for ch in env_channels]
    result += list(extra_channels.values())
    return {"channels": result}

@app.post("/api/channels")
async def api_add_channel(body: dict, secret: str = Query(...)):
    check_secret(secret)
    ch_id = int(body.get("id", 0))
    ch_name = str(body.get("name", str(ch_id)))
    if not ch_id:
        raise HTTPException(status_code=400, detail="id requerido")
    extra_channels[ch_id] = {"id": ch_id, "name": ch_name, "source": "dynamic", "active": True}
    _save_extra_channels()
    if _telethon_client:
        _telethon_client.add_event_handler(
            _make_handler(_telethon_client),
            events.NewMessage(chats=[ch_id])
        )
    _add_log(f"Canal agregado: {ch_name} ({ch_id})")
    log.info("Canal dinámico agregado: %s (%s)", ch_name, ch_id)
    return {"ok": True}

@app.delete("/api/channels/{channel_id}")
async def api_remove_channel(channel_id: int, secret: str = Query(...)):
    check_secret(secret)
    extra_channels.pop(channel_id, None)
    _save_extra_channels()
    _add_log(f"Canal eliminado: {channel_id}")
    return {"ok": True}

@app.post("/api/confirm/toggle")
async def api_toggle_confirm(secret: str = Query(...)):
    check_secret(secret)
    global confirm_required
    confirm_required = not confirm_required
    state = "con confirmación" if confirm_required else "auto-aprobación"
    _add_log(f"Modo cambiado: {state}")
    return {"confirm_required": confirm_required}

@app.post("/api/channels/{channel_id}/toggle")
async def api_toggle_channel(channel_id: int, secret: str = Query(...)):
    check_secret(secret)
    if channel_id not in extra_channels:
        raise HTTPException(status_code=404, detail="Canal no encontrado")
    extra_channels[channel_id]["active"] = not extra_channels[channel_id].get("active", True)
    _save_extra_channels()
    active = extra_channels[channel_id]["active"]
    _add_log(f"Canal {'activado' if active else 'pausado'}: {extra_channels[channel_id]['name']}")
    return {"id": channel_id, "active": active}

@app.get("/api/logs")
async def api_logs(secret: str = Query(...)):
    check_secret(secret)
    return {"logs": list(activity_log)}

@app.post("/debug/parse")
async def debug_parse(body: dict, secret: str = Query(...)):
    check_secret(secret)
    text = body.get("text", "")
    signal = parse_message(text)
    if signal is None:
        return {"parsed": False, "reason": "Formato no reconocido"}
    return {"parsed": True, "signal": {
        "type":      signal.type.value,
        "symbol":    signal.symbol_raw,
        "direction": signal.direction,
        "lots":      signal.lots,
        "price":     signal.price,
        "sl":        signal.sl,
        "tp":        signal.tp,
        "format":    signal.format,
    }}

# -------------------------------------------------------------------
# Confirmación via Telegram bot
# -------------------------------------------------------------------
async def send_confirmation(bot: Bot, signal: Signal, resolved_symbol: str, channel_name: str = "—"):
    global last_signal, signals_today
    callback_id = str(uuid.uuid4())[:8]

    signal_data = {
        "id":        callback_id,
        "action":    "open" if signal.type == SignalType.OPEN else "close",
        "symbol":    resolved_symbol,
        "direction": signal.direction,
        "sl":        signal.sl or 0.0,
        "tp":        signal.tp or 0.0,
        "pe":        signal.price,
        "status":    "pendiente",
        "trader":    signal.trader,
        "channel":   channel_name,
    }
    now = datetime.utcnow()
    signal_data["time"]     = now.strftime("%H:%M")
    signal_data["datetime"] = now.strftime("%d/%m/%Y %H:%M")
    pending_for_user[callback_id] = signal_data
    last_signal = signal_data
    signal_history.append(signal_data)
    signals_today += 1
    sl_info = f" SL={signal.sl}" if signal.sl else ""
    tp_info = f" TP={signal.tp}" if signal.tp else ""
    _add_log(f"{'OPEN' if signal.type == SignalType.OPEN else 'CLOSE'} {resolved_symbol} {signal.direction}{sl_info}{tp_info} — esperando aprobación")

    is_open = signal.type == SignalType.OPEN
    text = (
        notifier.format_open_message(signal, resolved_symbol, 0)
        if is_open
        else notifier.format_close_message(signal, resolved_symbol)
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{callback_id}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{callback_id}"),
    ]])

    await bot.send_message(
        chat_id=cfg["telegram"]["admin_chat_id"],
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    timeout = cfg["trading"].get("confirmation_timeout_seconds", 60)
    await asyncio.sleep(timeout)
    if callback_id in pending_for_user:
        del pending_for_user[callback_id]
        _add_log(f"Señal expirada — {resolved_symbol}")
        await bot.send_message(
            chat_id=cfg["telegram"]["admin_chat_id"],
            text=f"⏱ Señal expirada — {resolved_symbol}",
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_for_ea, last_signal, signals_executed, signal_history
    query = update.callback_query
    await query.answer()

    action, callback_id = query.data.split(":", 1)

    if callback_id not in pending_for_user:
        await query.edit_message_text("⚠️ Señal ya expirada o procesada.")
        return

    signal_data = pending_for_user.pop(callback_id)

    if action == "reject":
        _add_log(f"RECHAZADA — {signal_data['symbol']}")
        await query.edit_message_text(f"❌ Rechazada — {signal_data['symbol']}")
        return

    signal_data["status"] = "aprobada"
    pending_for_ea = signal_data
    last_signal = signal_data
    signals_executed += 1
    _add_log(f"APROBADA — {signal_data['symbol']} {signal_data.get('direction','')} → enviada al EA")
    await query.edit_message_text(
        f"✅ Aprobada — {signal_data['symbol']} {signal_data['direction']}\n"
        f"El EA ejecutará en el próximo ciclo.",
    )
    log.info("Señal aprobada y en cola para EA: %s", signal_data)

# -------------------------------------------------------------------
# Telethon
# -------------------------------------------------------------------
def _make_handler(client: TelegramClient):
    async def on_message(event):
        global pending_for_ea
        chat_id = event.chat_id
        if chat_id in extra_channels and not extra_channels[chat_id].get("active", True):
            return
        text   = event.raw_text
        signal = parse_message(text)
        if not signal:
            return

        symbol_suffix = cfg["mt5"].get("symbol_suffix", "")
        base     = signal.symbol_raw.split(".")[0]
        resolved = base + symbol_suffix

        # Nombre del canal origen
        if chat_id in extra_channels:
            channel_name = extra_channels[chat_id].get("name", str(chat_id))
        else:
            channel_name = str(chat_id)

        log.info("Señal: %s %s %s canal=%s", signal.type.value, resolved, signal.direction, channel_name)

        if signal.type == SignalType.OPEN:
            if confirm_required:
                asyncio.create_task(send_confirmation(_bot_ref, signal, resolved, channel_name))
            else:
                now = datetime.utcnow()
                pending_for_ea = {
                    "id":        str(uuid.uuid4())[:8],
                    "action":    "open",
                    "symbol":    resolved,
                    "direction": signal.direction,
                    "sl":        signal.sl or 0.0,
                    "tp":        signal.tp or 0.0,
                    "pe":        signal.price,
                    "status":    "auto-aprobada",
                    "trader":    signal.trader,
                    "channel":   channel_name,
                    "time":      now.strftime("%H:%M"),
                    "datetime":  now.strftime("%d/%m/%Y %H:%M"),
                }
                last_signal = pending_for_ea
                signal_history.append(pending_for_ea)
                signals_today += 1
                signals_executed += 1
                _add_log(f"AUTO-APROBADA {resolved} {signal.direction}")
                await _bot_ref.send_message(
                    chat_id=cfg["telegram"]["admin_chat_id"],
                    text=f"⚡ Auto-aprobada — {resolved} {signal.direction} @ {signal.price}",
                )
        elif signal.type == SignalType.CLOSE:
            close_confirm = cfg["trading"].get("close_requires_confirmation", False)
            if close_confirm:
                asyncio.create_task(send_confirmation(_bot_ref, signal, resolved))
            else:
                pending_for_ea = {
                    "id":     str(uuid.uuid4())[:8],
                    "action": "close",
                    "symbol": resolved,
                }
                _add_log(f"CLOSE automático → EA — {resolved}")
                await _bot_ref.send_message(
                    chat_id=cfg["telegram"]["admin_chat_id"],
                    text=f"🔴 Cierre automático enviado al EA — {resolved}",
                )
    return on_message

_bot_ref: Optional[Bot] = None

async def run_telethon(bot: Bot):
    global telethon_connected, _telethon_client, _bot_ref
    _bot_ref = bot

    from telethon.sessions import StringSession
    session_str = os.environ.get("TG_STRING_SESSION", "")
    session = StringSession(session_str) if session_str else "session_lumy"
    if session_str:
        log.info("Sesión Telethon cargada desde TG_STRING_SESSION")

    client = TelegramClient(session, int(cfg["telegram"]["api_id"]), cfg["telegram"]["api_hash"])
    _telethon_client = client
    await client.start()

    me = await client.get_me()
    log.info("Telethon conectado como %s", me.username)
    telethon_connected = True
    _add_log(f"Telethon conectado como @{me.username}")

    all_channels = cfg["telegram"]["source_channel"] + list(extra_channels.keys())
    client.add_event_handler(_make_handler(client), events.NewMessage(chats=all_channels))
    if extra_channels:
        log.info("Canales persistidos cargados: %s", list(extra_channels.keys()))

    await client.run_until_disconnected()
    telethon_connected = False

# -------------------------------------------------------------------
# Punto de entrada
# -------------------------------------------------------------------
async def run_all():
    global bot_running
    tg_app = Application.builder().token(cfg["telegram"]["bot_token"]).build()
    tg_app.add_handler(CallbackQueryHandler(handle_callback))

    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))

    async with tg_app:
        await tg_app.start()
        bot_running = True
        await tg_app.updater.start_polling()
        await asyncio.gather(
            run_telethon(tg_app.bot),
            server.serve(),
        )
        await tg_app.updater.stop()
        await tg_app.stop()
        bot_running = False

if __name__ == "__main__":
    asyncio.run(run_all())
