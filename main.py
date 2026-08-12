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

import httpx
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
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
CHANNELS_PATH  = DATA_DIR / "channels.json"
LOG_PATH       = DATA_DIR / "activity_log.json"
STATS_PATH     = DATA_DIR / "channel_stats.json"
LEARNED_PATH   = DATA_DIR / "learned_formats.json"

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        ch = cfg["telegram"]["source_channel"]
        if not isinstance(ch, list):
            cfg["telegram"]["source_channel"] = [ch]
        return cfg

    log.info("config.json no encontrado — usando variables de entorno")
    # TG_CHANNEL_NAMES = "-100123:LumyArena,-100456:OtroCanal"
    channel_names: dict[int, str] = {}
    raw_names = os.environ.get("TG_CHANNEL_NAMES", "")
    for part in raw_names.split(","):
        part = part.strip()
        if ":" in part:
            cid, cname = part.split(":", 1)
            try:
                channel_names[int(cid.strip())] = cname.strip()
            except ValueError:
                pass

    return {
        "telegram": {
            "api_id":         os.environ["TG_API_ID"],
            "api_hash":       os.environ["TG_API_HASH"],
            "source_channel": [int(x.strip()) for x in os.environ.get("TG_SOURCE_CHANNEL", "").split(",") if x.strip()],
            "bot_token":      os.environ["TG_BOT_TOKEN"],
            "admin_chat_id":  os.environ["TG_ADMIN_CHAT_ID"],
            "channel_names":  channel_names,
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
channel_stats:    dict[int, dict] = {}  # {chat_id: {total, buys, sells, history}}
signals_executed: int             = 0
telethon_connected: bool          = False
bot_running:      bool            = False
activity_log: deque               = deque(maxlen=100)
confirm_required: bool            = False  # True = pedir confirmación, False = auto-aprobar

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

def _load_activity_log() -> list:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text())
        except Exception:
            pass
    return []

def _save_activity_log():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(json.dumps(list(activity_log)))
    except Exception as e:
        log.warning("No se pudo guardar activity_log.json: %s", e)

def _load_channel_stats() -> dict:
    if STATS_PATH.exists():
        try:
            return {int(k): v for k, v in json.loads(STATS_PATH.read_text()).items()}
        except Exception:
            pass
    return {}

def _save_channel_stats():
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(json.dumps(channel_stats))
    except Exception as e:
        log.warning("No se pudo guardar channel_stats.json: %s", e)

def _rebuild_from_channel_stats():
    """Reconstruye contadores y signal_history desde channel_stats persistido."""
    global signals_today, signals_executed, last_signal
    all_signals = []
    for stats in channel_stats.values():
        signals_today += stats.get("total", 0)
        for sig in stats.get("history", []):
            all_signals.append(sig)
            if sig.get("status") in ("aprobada", "auto-aprobada"):
                signals_executed += 1
    if all_signals:
        all_signals.sort(key=lambda s: s.get("datetime", ""), reverse=True)
        for sig in reversed(all_signals[:20]):
            signal_history.append(sig)
        last_signal = all_signals[0]

cfg = load_config()
extra_channels = _load_extra_channels()
for entry in _load_activity_log():
    activity_log.append(entry)
channel_stats.update(_load_channel_stats())
_rebuild_from_channel_stats()
_telethon_client: Optional[TelegramClient] = None

def _normalize_id(chat_id: int) -> int:
    """Normaliza el ID del canal: siempre positivo para comparar."""
    return abs(chat_id)

def _update_channel_stats(chat_id: int, signal_data: dict):
    key = _normalize_id(chat_id)
    if key not in channel_stats:
        channel_stats[key] = {"total": 0, "buys": 0, "sells": 0, "history": []}
    s = channel_stats[key]
    s["total"] += 1
    if signal_data.get("direction") == "BUY":
        s["buys"] += 1
    elif signal_data.get("direction") == "SELL":
        s["sells"] += 1
    s["history"] = ([signal_data] + s["history"])[:20]
    _save_channel_stats()

def _add_log(msg: str):
    ts = datetime.utcnow().strftime("%d/%m %H:%M")
    activity_log.append(f"{ts} {msg}")
    _save_activity_log()

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
_ea_last_poll: Optional[datetime] = None

@app.get("/signal/pending")
async def get_pending_signal(secret: str = Query(...)):
    global _ea_last_poll
    check_secret(secret)
    _ea_last_poll = datetime.utcnow()
    if pending_for_ea is None:
        return None
    return pending_for_ea

@app.post("/signal/ack/{signal_id}")
async def ack_signal(signal_id: str, secret: str = Query(...)):
    check_secret(secret)
    global pending_for_ea
    if pending_for_ea and pending_for_ea.get("id") == signal_id:
        log.info("EA confirmó alerta %s", signal_id)
        _add_log(f"EA ejecutó alerta {signal_id}")
        pending_for_ea = None
    return {"ok": True}

@app.post("/signal/result")
async def signal_result(body: dict, secret: str = Query(...)):
    check_secret(secret)
    signal_id = body.get("id", "")
    executed  = body.get("executed", False)
    detail    = body.get("detail", "")
    ticket    = body.get("ticket", "")
    price     = body.get("price", "")

    # Actualizar estado en historial en memoria
    for s in signal_history:
        if s.get("id") == signal_id:
            s["status"] = "ejecutada" if executed else "no ejecutada"
            if detail: s["mt5_detail"] = detail
            if ticket: s["mt5_ticket"] = str(ticket)
            break

    # Actualizar en channel_stats y persistir
    for ch_data in channel_stats.values():
        for s in ch_data.get("history", []):
            if s.get("id") == signal_id:
                s["status"] = "ejecutada" if executed else "no ejecutada"
                if detail: s["mt5_detail"] = detail
                if ticket: s["mt5_ticket"] = str(ticket)
                break
    _save_channel_stats()

    # Notificar por Telegram
    if executed:
        msg = f"✅ *Alerta ejecutada en MT5*\nTicket: `{ticket}` | Precio real: `{price}`"
    else:
        msg = f"⚠️ *Alerta NO ejecutada*\nMotivo: {detail}"
    _add_log(("✅ Ejecutada" if executed else "⚠️ No ejecutada") + f" — {signal_id} {detail}")
    if _bot_ref:
        await _alert(msg)

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
        "ea_connected":       _ea_last_poll is not None and (datetime.utcnow() - _ea_last_poll).total_seconds() < 30,
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
    empty_stats = lambda: {"total": 0, "buys": 0, "sells": 0, "history": []}
    env_names = cfg["telegram"].get("channel_names", {})
    env_channels = cfg["telegram"]["source_channel"]
    result = [{"id": ch, "name": env_names.get(ch, env_names.get(abs(ch), f"Instrumento {abs(ch)}")),
               "source": "env",
               "stats": channel_stats.get(abs(ch), empty_stats())}
              for ch in env_channels]
    for ch in extra_channels.values():
        ch_copy = dict(ch)
        ch_copy["stats"] = channel_stats.get(abs(ch["id"]), empty_stats())
        result.append(ch_copy)
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
    _add_log(f"Instrumento agregado: {ch_name} ({ch_id})")
    log.info("Instrumento agregado: %s (%s)", ch_name, ch_id)
    return {"ok": True}

@app.delete("/api/channels/{channel_id}")
async def api_remove_channel(channel_id: int, secret: str = Query(...)):
    check_secret(secret)
    extra_channels.pop(channel_id, None)
    _save_extra_channels()
    _add_log(f"Instrumento eliminado: {channel_id}")
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
        raise HTTPException(status_code=404, detail="Instrumento no encontrado")
    extra_channels[channel_id]["active"] = not extra_channels[channel_id].get("active", True)
    _save_extra_channels()
    active = extra_channels[channel_id]["active"]
    _add_log(f"Instrumento {'activado' if active else 'pausado'}: {str(abs(channel_id))[-5:]}")
    return {"id": channel_id, "active": active}

@app.get("/api/logs")
async def api_logs(secret: str = Query(...)):
    check_secret(secret)
    return {"logs": list(activity_log)}

@app.patch("/api/signal/{signal_id}/annotation")
async def api_annotate_signal(signal_id: str, body: dict, secret: str = Query(...)):
    check_secret(secret)
    found = False
    for stats in channel_stats.values():
        for sig in stats.get("history", []):
            if sig.get("id") == signal_id:
                if "result" in body:
                    sig["result"] = body["result"]
                if "note" in body:
                    sig["note"] = body["note"]
                found = True
                break
        if found:
            break
    for sig in signal_history:
        if sig.get("id") == signal_id:
            if "result" in body:
                sig["result"] = body["result"]
            if "note" in body:
                sig["note"] = body["note"]
            break
    _save_channel_stats()
    return {"ok": found}

_AI_PROMPT = """Eres un parser de señales de trading de canales de Telegram.
Analiza el mensaje y extrae la señal de trading si existe.

Responde SOLO con JSON válido, sin explicaciones ni markdown.

Si es señal de APERTURA (nueva entrada):
{"type":"open","symbol":"XAUUSD","direction":"BUY","entry":3320.0,"sl":3300.0,"tp":3360.0}

Si es señal de CIERRE (cerrar posición):
{"type":"close","symbol":"XAUUSD","direction":"SELL","pips":60}

Si NO es una señal de trading:
null

Reglas:
- Palabras de cierre: CIERREN, CIERRE, CERRAR, CLOSE, CLOSED, TP HIT, SL HIT, +X PIPS CIERREN, salir
- Palabras de apertura: BUY, SELL, ENTRY, ABRIR, NOW, nueva entrada, COMPRAR, COMPRA, VENDER, VENTA
- symbol: sin barras (XAUUSD no XAU/USD), en mayúsculas
- tp/sl: null si dice NINGUNO, NONE, NO TP, NO SL o no menciona
- Si el mensaje menciona un trade previo Y luego dice CIERREN → es CIERRE de ese trade
- direction en cierre = dirección del trade original que se cierra

Mensaje:
{text}"""

def _load_learned_formats() -> list:
    try:
        if LEARNED_PATH.exists():
            return json.loads(LEARNED_PATH.read_text())
    except Exception:
        pass
    return []

def _build_ai_prompt(text: str) -> str:
    learned = _load_learned_formats()
    examples = ""
    if learned:
        examples = "\n\nEjemplos de formatos ya reconocidos (úsalos como referencia):\n"
        for f in learned[-8:]:  # últimos 8 ejemplos
            s = f.get("signal", {})
            examples += f'Mensaje: """{f["text"][:200]}"""\n'
            examples += f'Resultado: {json.dumps(s, ensure_ascii=False)}\n\n'
    return _AI_PROMPT.format(text=text) + examples

async def _parse_with_ai(text: str) -> Optional[Signal]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": _build_ai_prompt(text)}],
                },
            )
        if r.status_code != 200:
            log.warning("AI parser HTTP %s", r.status_code)
            return None
        raw = r.json()["content"][0]["text"].strip()
        if not raw or raw.lower() == "null":
            return None
        data = json.loads(raw)
        sig_type = SignalType.CLOSE if data.get("type") == "close" else SignalType.OPEN
        from parser import normalize_symbol, _guess_trader
        symbol = normalize_symbol(data.get("symbol", ""))
        return Signal(
            type=sig_type,
            trader=_guess_trader(text),
            symbol_raw=symbol,
            direction=data.get("direction", "BUY").upper(),
            lots=0.0,
            price=float(data.get("entry") or data.get("close_price") or 0.0),
            sl=float(data["sl"]) if data.get("sl") else None,
            tp=float(data["tp"]) if data.get("tp") else None,
            format="ai",
            raw_text=text,
        )
    except Exception as e:
        log.warning("AI parser error: %s", e)
        return None

async def parse_signal(text: str) -> Optional[Signal]:
    """Intenta regex primero; si falla, usa IA como fallback."""
    signal = parse_message(text)
    if signal is None:
        signal = await _parse_with_ai(text)
    return signal

@app.post("/debug/parse")
async def debug_parse(body: dict, secret: str = Query(...)):
    check_secret(secret)
    text = body.get("text", "")
    signal = await parse_signal(text)
    if signal is None:
        has_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
        reason = "Formato no reconocido"
        if not has_ai:
            reason += " — IA no activa (falta ANTHROPIC_API_KEY en Railway)"
        return {"parsed": False, "reason": reason, "ai_available": has_ai}
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

_SUGGEST_PROMPT = """Eres un experto en señales de trading de Telegram.

Analiza este mensaje e intenta extraer una señal de trading. Sé inteligente:
- Si no hay símbolo explícito, infíerelo por el rango de precio (ej: 4200-4400 → XAUUSD, 1.08 → EURUSD, 40000 → DJ30ft/US30)
- Si no hay BUY/SELL explícito, inferirlo por la posición del SL vs TP vs entrada (TP > entrada → BUY, TP < entrada → SELL)
- 📍 o 🎯 suelen indicar el precio de entrada
- ✅ o 💰 suelen indicar Take Profit
- 🔴 o 🚫 suelen indicar Stop Loss
- LONG = BUY, SHORT = SELL, COMPRAR/COMPRA = BUY, VENDER/VENTA = SELL

Responde SOLO con JSON válido, sin explicaciones. Campos:
- type: "open" o "close"
- symbol: símbolo en mayúsculas sin barras (XAUUSD, EURUSD, DJ30ft, etc.)
- direction: "BUY" o "SELL"
- entry: precio de entrada (primer número del rango si hay rango), 0 si es mercado
- sl: stop loss como número, null si no hay
- tp: take profit primario (TP1 si hay varios), null si no hay
- confidence: "high", "medium" o "low" según qué tan seguro estás

Si definitivamente no es una señal de trading: null

Mensaje:
{text}"""

async def _parse_suggest(text: str) -> Optional[dict]:
    """Parser más permisivo para el tester — infiere datos faltantes."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": _SUGGEST_PROMPT.format(text=text)}],
                },
            )
        if r.status_code != 200:
            return None
        raw = r.json()["content"][0]["text"].strip()
        if not raw or raw.lower() == "null":
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("_parse_suggest error: %s", e)
        return None

@app.post("/debug/suggest-parser")
async def suggest_parser(body: dict, secret: str = Query(...)):
    check_secret(secret)
    text = body.get("text", "")
    if not text:
        return {"ok": False, "reason": "Texto vacío"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "reason": "Falta ANTHROPIC_API_KEY en Railway"}
    data = await _parse_suggest(text)
    if data is None:
        return {"ok": False, "reason": "Claude no pudo interpretar este mensaje como alerta de trading"}
    signal = {
        "type":      data.get("type", "open"),
        "symbol":    data.get("symbol", ""),
        "direction": data.get("direction", "BUY"),
        "entry":     float(data.get("entry") or 0),
        "sl":        float(data["sl"]) if data.get("sl") else None,
        "tp":        float(data["tp"]) if data.get("tp") else None,
    }
    return {"ok": True, "signal": signal, "confidence": data.get("confidence", "medium"), "text": text}

@app.post("/debug/learn-format")
async def learn_format(body: dict, secret: str = Query(...)):
    check_secret(secret)
    text   = body.get("text", "")
    signal = body.get("signal", {})
    if not text or not signal:
        return {"ok": False, "reason": "Faltan datos"}
    learned = _load_learned_formats()
    learned.append({
        "text":   text,
        "signal": signal,
        "added":  datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
    })
    LEARNED_PATH.write_text(json.dumps(learned, ensure_ascii=False, indent=2))
    _add_log(f"✅ Formato aprendido: {signal.get('symbol','?')} {signal.get('direction','?')}")
    return {"ok": True, "total": len(learned)}

# -------------------------------------------------------------------
# Confirmación via Telegram bot
# -------------------------------------------------------------------
async def send_confirmation(bot: Bot, signal: Signal, resolved_symbol: str, channel_name: str = "—", channel_id: int = 0):
    global last_signal, signals_today
    callback_id = str(uuid.uuid4())[:8]

    signal_data = {
        "id":         callback_id,
        "action":     "open" if signal.type == SignalType.OPEN else "close",
        "symbol":     resolved_symbol,
        "direction":  signal.direction,
        "sl":         signal.sl or 0.0,
        "tp":         signal.tp or 0.0,
        "pe":         signal.price,
        "pe_low":     signal.price_low or 0.0,
        "status":     "pendiente",
        "trader":     signal.trader,
        "channel":    channel_name,
        "channel_id": abs(channel_id),
    }
    now = datetime.utcnow()
    signal_data["time"]     = now.strftime("%H:%M")
    signal_data["datetime"] = now.strftime("%d/%m/%Y %H:%M")
    pending_for_user[callback_id] = signal_data
    last_signal = signal_data
    signal_history.append(signal_data)
    signals_today += 1
    # Pasar el MISMO dict a channel_stats para que al mutar el estado se refleje aquí
    if channel_id:
        _update_channel_stats(channel_id, signal_data)
    sl_info = f" SL={signal.sl}" if signal.sl else ""
    tp_info = f" TP={signal.tp}" if signal.tp else ""
    _add_log(f"{'OPEN' if signal.type == SignalType.OPEN else 'CLOSE'} {resolved_symbol} {signal.direction}{sl_info}{tp_info} — esperando aprobación")

    is_open = signal.type == SignalType.OPEN
    text = (
        notifier.format_open_message(signal, resolved_symbol, 0, channel_name)
        if is_open
        else notifier.format_close_message(signal, resolved_symbol, channel_name)
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
        _add_log(f"Alerta expirada — {resolved_symbol}")
        await bot.send_message(
            chat_id=cfg["telegram"]["admin_chat_id"],
            text=f"⏱ Alerta expirada — {resolved_symbol}",
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_for_ea, last_signal, signals_executed, signal_history
    query = update.callback_query
    await query.answer()

    action, callback_id = query.data.split(":", 1)

    if callback_id not in pending_for_user:
        await query.edit_message_text("⚠️ Alerta ya expirada o procesada.")
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
    log.info("Alerta aprobada y en cola para EA: %s", signal_data)

# -------------------------------------------------------------------
# Telethon
# -------------------------------------------------------------------
def _make_handler(client: TelegramClient):
    async def on_message(event):
        global pending_for_ea, last_signal, signals_today, signals_executed
        chat_id = event.chat_id
        if chat_id in extra_channels and not extra_channels[chat_id].get("active", True):
            return
        text   = event.raw_text
        signal = await parse_signal(text)
        if not signal:
            return

        symbol_suffix = cfg["mt5"].get("symbol_suffix", "")
        base     = signal.symbol_raw.split(".")[0]
        resolved = base + symbol_suffix

        # Nombre del canal origen (normalizar ID: Telethon puede dar negativo)
        norm_id = _normalize_id(chat_id)
        if chat_id in extra_channels:
            channel_name = extra_channels[chat_id].get("name", str(norm_id))
        elif norm_id in extra_channels:
            channel_name = extra_channels[norm_id].get("name", str(norm_id))
        else:
            env_names = cfg["telegram"].get("channel_names", {})
            channel_name = env_names.get(chat_id, env_names.get(norm_id, str(norm_id)))

        log.info("Alerta: %s %s %s instrumento=%s", signal.type.value, resolved, signal.direction, channel_name)

        if signal.type == SignalType.OPEN:
            if confirm_required:
                asyncio.create_task(send_confirmation(_bot_ref, signal, resolved, channel_name, chat_id))
            else:
                now = datetime.utcnow()
                pending_for_ea = {
                    "id":         str(uuid.uuid4())[:8],
                    "action":     "open",
                    "symbol":     resolved,
                    "direction":  signal.direction,
                    "sl":         signal.sl or 0.0,
                    "tp":         signal.tp or 0.0,
                    "pe":         signal.price,
                    "pe_low":     signal.price_low or 0.0,
                    "status":     "auto-aprobada",
                    "trader":     signal.trader,
                    "channel":    channel_name,
                    "channel_id": norm_id,
                    "time":       now.strftime("%H:%M"),
                    "datetime":   now.strftime("%d/%m/%Y %H:%M"),
                }
                last_signal = pending_for_ea
                signal_history.append(pending_for_ea)
                signals_today += 1
                signals_executed += 1
                _update_channel_stats(chat_id, pending_for_ea)
                _add_log(f"AUTO-APROBADA {resolved} {signal.direction} [{str(norm_id)[-5:]}]")
                await _bot_ref.send_message(
                    chat_id=cfg["telegram"]["admin_chat_id"],
                    text=f"⚡ Auto-aprobada — {resolved} {signal.direction} @ {signal.price}\n📢 Instrumento: {channel_name}",
                )
        elif signal.type == SignalType.CLOSE:
            close_confirm = cfg["trading"].get("close_requires_confirmation", False)
            if close_confirm:
                asyncio.create_task(send_confirmation(_bot_ref, signal, resolved))
            else:
                pending_for_ea = {
                    "id":         str(uuid.uuid4())[:8],
                    "action":     "close",
                    "symbol":     resolved,
                    "channel_id": norm_id,
                }
                _add_log(f"CLOSE automático → EA — {resolved}")
                await _bot_ref.send_message(
                    chat_id=cfg["telegram"]["admin_chat_id"],
                    text=f"🔴 Cierre automático enviado al EA — {resolved}\n📢 Instrumento: {channel_name}",
                )
    return on_message

_bot_ref: Optional[Bot] = None

async def _alert(text: str):
    """Envía alerta al admin vía bot. No lanza excepción si falla."""
    if not _bot_ref:
        return
    try:
        await _bot_ref.send_message(chat_id=cfg["telegram"]["admin_chat_id"], text=text)
    except Exception as e:
        log.warning("No se pudo enviar alerta: %s", e)

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
    _add_log(f"TradingView conectado como @{me.username}")
    await _alert(f"✅ MevoTrader online — TradingView conectado como @{me.username}")

    all_channels = cfg["telegram"]["source_channel"] + list(extra_channels.keys())
    client.add_event_handler(_make_handler(client), events.NewMessage(chats=all_channels))
    if extra_channels:
        log.info("Canales persistidos cargados: %s", list(extra_channels.keys()))

    await client.run_until_disconnected()
    telethon_connected = False

async def run_telethon_with_retry(bot: Bot):
    """Loop de reconexión con backoff exponencial. Alerta al admin en cada evento."""
    _MIN_DELAY = 5
    _MAX_DELAY = 300  # 5 minutos
    delay = _MIN_DELAY
    first_run = True

    while True:
        try:
            if not first_run:
                log.info("Reconectando Telethon en %ss...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_DELAY)

            await run_telethon(bot)

            # run_telethon retornó = desconexión limpia
            log.warning("Telethon desconectado (salida limpia)")
            _add_log("⚠️ TradingView desconectado — reconectando...")
            await _alert(f"⚠️ MevoTrader: TradingView desconectado.\nReconectando en {delay}s...")

        except Exception as e:
            log.error("Telethon error: %s", e)
            _add_log(f"❌ TradingView error: {type(e).__name__}")
            await _alert(f"❌ MevoTrader: TradingView falló ({type(e).__name__})\nReconectando en {delay}s...")

        finally:
            telethon_connected = False
            first_run = False

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
            run_telethon_with_retry(tg_app.bot),
            server.serve(),
        )
        await tg_app.updater.stop()
        await tg_app.stop()
        bot_running = False

if __name__ == "__main__":
    asyncio.run(run_all())
