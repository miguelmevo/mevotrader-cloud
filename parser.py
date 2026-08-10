import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

def normalize_symbol(raw: str) -> str:
    """Elimina '/' del símbolo — la homologación la hace el EA."""
    return raw.replace("/", "").upper()


class SignalType(Enum):
    OPEN = "open"
    CLOSE = "close"
    UNKNOWN = "unknown"


@dataclass
class Signal:
    type: SignalType
    trader: str
    symbol_raw: str
    direction: str        # BUY / SELL
    lots: float
    price: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    pips: Optional[float] = None
    profit: Optional[str] = None
    price_low: Optional[float] = None   # límite inferior del rango de entrada
    format: str = "unknown"
    raw_text: str = ""


def parse_message(text: str) -> Optional[Signal]:
    """Intenta parsear el texto con múltiples formatos conocidos."""
    text = text.strip()

    # --- Formato LumyArena (español) ---
    if "acaba de abrir una posición" in text:
        return _parse_lumy_open(text)
    if "acaba de cerrar una posición" in text:
        return _parse_lumy_close(text)

    # --- Formato LONG/SHORT en (SYMBOL Activo) ---
    m = re.search(r'\b(LONG|SHORT)\s+en\s+\(([A-Z0-9./]{2,10})\s+Activo\)', text, re.IGNORECASE)
    if m:
        return _parse_long_short(text, m)

    # --- Formato genérico: BUY/SELL TRADE SYMBOL ---
    m = re.search(r'\b(BUY|SELL)\s+TRADE\s+([\w.]+)', text, re.IGNORECASE)
    if m:
        return _parse_trade_signal(text, m)

    # --- Formato SYMBOL BUY/SELL NOW  (ej: "US30 BUY NOW") ---
    # También acepta COMPRAR/VENDER en español
    m = re.search(r'\b([\w./]{2,10})\s+(BUY|SELL|COMPRAR|VENDER|COMPRA|VENTA)(?:\s+NOW)?\b', text, re.IGNORECASE)
    if m:
        return _parse_now_signal(text, m)

    # --- Formato DIRECTION SYMBOL PRICE  (ej: "SELL XAUUSD 4138\nSL 4160\nTP NINGUNO") ---
    # El precio puede ir seguido de más contenido en la misma línea
    m = re.search(r'^(BUY|SELL)\s+([\w./]{2,10})\s+([\d.]{2,12})',
                  text, re.IGNORECASE | re.MULTILINE)
    if m:
        return _parse_dir_symbol_price(text, m)

    # --- Formato BUY/SELL SYMBOL ... Entrada: PRICE[-PRICE2]  (español, con rango) ---
    m = re.search(r'^(BUY|SELL)\s+([\w./]{2,10})', text, re.IGNORECASE | re.MULTILINE)
    if m and re.search(r'Entrada\s*[:\s]', text, re.IGNORECASE):
        return _parse_entrada_signal(text, m)

    # --- Formato genérico: BUY/SELL SYMBOL (sin TRADE) ---
    m = re.search(r'^(BUY|SELL)\s+([\w.]+)', text, re.IGNORECASE | re.MULTILINE)
    if m and re.search(r'ENTRY\s*[:\s]', text, re.IGNORECASE):
        return _parse_trade_signal(text, m)

    # --- Formato: SYMBOL BUY/SELL @ price ---
    m = re.search(r'([\w.]+)\s+(BUY|SELL)\s*@?\s*([\d.]+)', text, re.IGNORECASE)
    if m:
        return _parse_at_signal(text, m)

    return None


# ──────────────────────────────────────────
# LumyArena
# ──────────────────────────────────────────

def _lumy_trader(text: str) -> str:
    m = re.search(r'([\w]+)\s+acaba de (?:abrir|cerrar)', text)
    return m.group(1) if m else "desconocido"


def _lumy_symbol_dir_lots(text: str):
    m = re.search(r'([\w.]+)\s*[—\-–]\s*(BUY|SELL)\s+([\d.]+)\s+lots?', text, re.IGNORECASE)
    if not m:
        return None, None, 0.0
    return normalize_symbol(m.group(1)), m.group(2).upper(), float(m.group(3))


def _parse_lumy_open(text: str) -> Optional[Signal]:
    trader = _lumy_trader(text)
    symbol, direction, lots = _lumy_symbol_dir_lots(text)
    if not symbol:
        return None
    m = re.search(r'Precio\s*:\s*([\d.]+)', text)
    if not m:
        return None
    return Signal(
        type=SignalType.OPEN, trader=trader, symbol_raw=symbol,
        direction=direction, lots=lots, price=float(m.group(1)),
        format="lumy", raw_text=text,
    )


def _parse_lumy_close(text: str) -> Optional[Signal]:
    trader = _lumy_trader(text)
    symbol, direction, lots = _lumy_symbol_dir_lots(text)
    if not symbol:
        return None

    open_price = close_price = pips = profit = None

    m = re.search(r'Precio de apertura\s*:\s*([\d.]+)', text)
    if m: open_price = float(m.group(1))

    m = re.search(r'Precio de cierre\s*:\s*([\d.]+)', text)
    if m: close_price = float(m.group(1))

    m = re.search(r'Variaci[oó]n\s*:\s*([+-]?[\d.]+)\s*pips', text, re.IGNORECASE)
    if m: pips = float(m.group(1))

    m = re.search(r'Beneficio\s*:\s*([+-]?\$?[\d.,]+)', text, re.IGNORECASE)
    if m: profit = m.group(1)

    return Signal(
        type=SignalType.CLOSE, trader=trader, symbol_raw=symbol,
        direction=direction, lots=lots, price=close_price or 0.0,
        open_price=open_price, close_price=close_price,
        pips=pips, profit=profit, format="lumy", raw_text=text,
    )


# ──────────────────────────────────────────
# Formato BUY/SELL TRADE SYMBOL  /  BUY/SELL SYMBOL + ENTRY
# ──────────────────────────────────────────

def _parse_trade_signal(text: str, m) -> Optional[Signal]:
    direction = m.group(1).upper()
    symbol    = normalize_symbol(m.group(2))

    entry = _find_price(text, r'ENTRY\s*[:\s]\s*([\d.]+)')
    if entry is None:
        entry = _find_price(text, r'^([\d]{3,6}(?:\.\d+)?)\s*$', re.MULTILINE)
    if entry is None:
        entry = _find_price(text, r'([\d]{3,6}(?:\.\d+)?)')  # fallback
    if entry is None:
        return None

    # SL: SL, S/L, Stop Loss, StopLoss
    sl = _find_price(text, r'(?:S/?L|Stop\s*Loss)\s*[:\s]\s*([\d.]+)')
    # TP: TP, TP1, TP2, T/P, Take Profit, Take Profit 1, Target — ignora "open"
    tp_m = re.search(
        r'(?:T/?P\d*|Take\s*Profit\s*\d*|Target)\s*[:\s]\s*([\d.]+|open)',
        text, re.IGNORECASE
    )
    tp = float(tp_m.group(1)) if tp_m and tp_m.group(1).lower() != 'open' else None

    # Detectar cierre (inglés y español)
    is_close = bool(re.search(
        r'\b(close|closed|cerrad|tp hit|sl hit|hit tp|hit sl|cierren|cierra|cerrar|salgan|salir|exit)\b',
        text, re.IGNORECASE))

    return Signal(
        type=SignalType.CLOSE if is_close else SignalType.OPEN,
        trader=_guess_trader(text),
        symbol_raw=symbol,
        direction=direction,
        lots=0.0,
        price=entry,
        sl=sl,
        tp=tp,
        format="trade_signal",
        raw_text=text,
    )


# ──────────────────────────────────────────
# Formato SYMBOL BUY/SELL @ price
# ──────────────────────────────────────────

def _parse_at_signal(text: str, m) -> Optional[Signal]:
    symbol    = normalize_symbol(m.group(1))
    direction = m.group(2).upper()
    price     = float(m.group(3))

    sl = _find_price(text, r'SL\s*[:\s@]\s*([\d.]+)')
    tp_m = re.search(r'TP\s*[:\s@]\s*([\d.]+)', text, re.IGNORECASE)
    tp = float(tp_m.group(1)) if tp_m else None

    return Signal(
        type=SignalType.OPEN,
        trader=_guess_trader(text),
        symbol_raw=symbol,
        direction=direction,
        lots=0.0,
        price=price,
        sl=sl,
        tp=tp,
        format="at_signal",
        raw_text=text,
    )


# ──────────────────────────────────────────
# Formato SYMBOL BUY/SELL NOW  (ImperiumFX, etc.)
# ──────────────────────────────────────────

_DIR_MAP = {"COMPRAR": "BUY", "COMPRA": "BUY", "VENDER": "SELL", "VENTA": "SELL",
            "LONG": "BUY", "SHORT": "SELL"}

def _parse_now_signal(text: str, m) -> Optional[Signal]:
    symbol    = normalize_symbol(m.group(1))
    direction = _DIR_MAP.get(m.group(2).upper(), m.group(2).upper())
    # SL: acepta "SL:", "Stop Loss:", "Stop loss:"
    sl = _find_price(text, r'(?:S/?L|Stop\s*Loss)\s*[:\s]\s*([\d.]+)')
    # TP: acepta "TP:", "TP1:", "Take profit 1:", etc.
    tp_m = re.search(r'(?:T/?P\d*|Take\s*[Pp]rofit\s*\d*)\s*[:\s]\s*([\d.]+)', text, re.IGNORECASE)
    tp = float(tp_m.group(1)) if tp_m else None
    # Precio de entrada desde rango "📍 4356.81-4337.08" o "Entrada: 4356"
    entry_m = re.search(r'(?:📍|🎯|Entrada\s*[:\s])\s*([\d.]+)', text)
    price = float(entry_m.group(1)) if entry_m else 0.0
    return Signal(
        type=SignalType.OPEN, trader=_guess_trader(text),
        symbol_raw=symbol, direction=direction,
        lots=0.0, price=price,
        sl=sl, tp=tp,
        format="now_signal", raw_text=text,
    )


# ──────────────────────────────────────────
# Formato DIRECTION SYMBOL PRICE  (PapaEnParis, etc.)
# ──────────────────────────────────────────

_CLOSE_RE = re.compile(
    r'\b(close|closed|cerrad|tp hit|sl hit|hit tp|hit sl|cierren|cierra|cerrar|salgan|salir|exit)\b',
    re.IGNORECASE)

def _parse_dir_symbol_price(text: str, m) -> Optional[Signal]:
    direction = m.group(1).upper()
    symbol    = normalize_symbol(m.group(2))
    price     = float(m.group(3))

    # SL: busca "SL 4200" o "S/L: 4200" en cualquier parte del texto
    sl = _find_price(text, r'\bS/?L\s*[:\s]\s*([\d.]+)')
    # TP: acepta NINGUNO/NONE/- como sin TP
    tp_m = re.search(r'\bT/?P\s*[:\s]\s*([\d.]+|NINGUNO|NONE|-)', text, re.IGNORECASE)
    tp = None
    if tp_m:
        raw = tp_m.group(1).upper()
        if raw not in ('NINGUNO', 'NONE', '-'):
            try:
                tp = float(raw)
            except ValueError:
                pass

    # Si el texto contiene palabras de cierre → es un CLOSE del trade referenciado
    is_close = bool(_CLOSE_RE.search(text))

    return Signal(
        type=SignalType.CLOSE if is_close else SignalType.OPEN,
        trader=_guess_trader(text),
        symbol_raw=symbol, direction=direction,
        lots=0.0, price=price,
        sl=sl, tp=tp,
        format="dir_symbol_price", raw_text=text,
    )


# ──────────────────────────────────────────
# Formato LONG/SHORT en (SYMBOL Activo)
# ──────────────────────────────────────────

def _parse_long_short(text: str, m) -> Optional[Signal]:
    direction = _DIR_MAP.get(m.group(1).upper(), m.group(1).upper())
    symbol    = normalize_symbol(m.group(2))
    sl = _find_price(text, r'\bS/?L\s*[:\s]\s*([\d.]+)')
    tp_m = re.search(r'\bTP1?\s*[:\s]\s*([\d.]+)', text, re.IGNORECASE)
    tp = float(tp_m.group(1)) if tp_m else None
    entry_m = re.search(r'Entrada\s*[:\s]\s*([\d.]+)', text, re.IGNORECASE)
    price = float(entry_m.group(1)) if entry_m else 0.0
    is_close = bool(_CLOSE_RE.search(text))
    return Signal(
        type=SignalType.CLOSE if is_close else SignalType.OPEN,
        trader=_guess_trader(text),
        symbol_raw=symbol, direction=direction,
        lots=0.0, price=price,
        sl=sl, tp=tp,
        format="long_short", raw_text=text,
    )


# ──────────────────────────────────────────
# Formato BUY/SELL SYMBOL ... Entrada: PRICE[-PRICE2]  (español, canales latam)
# ──────────────────────────────────────────

def _parse_entrada_signal(text: str, m) -> Optional[Signal]:
    direction = m.group(1).upper()
    symbol    = normalize_symbol(m.group(2))

    # Acepta "Entrada: 4165" o "Entrada: 4165-4163" — captura rango si existe
    entry_m = re.search(r'Entrada\s*[:\s]\s*([\d.]+)(?:\s*-\s*([\d.]+))?', text, re.IGNORECASE)
    if not entry_m:
        return None
    price = float(entry_m.group(1))
    price_low = float(entry_m.group(2)) if entry_m.group(2) else None

    sl = _find_price(text, r'\bS/?L\s*[:\s]\s*([\d.]+)')
    # TP1 como TP primario; si no hay TP1 busca TP genérico
    tp_m = re.search(r'\bTP1?\s*[:\s]\s*([\d.]+)', text, re.IGNORECASE)
    tp = float(tp_m.group(1)) if tp_m else None

    is_close = bool(_CLOSE_RE.search(text))

    return Signal(
        type=SignalType.CLOSE if is_close else SignalType.OPEN,
        trader=_guess_trader(text),
        symbol_raw=symbol, direction=direction,
        lots=0.0, price=price, price_low=price_low,
        sl=sl, tp=tp,
        format="entrada_signal", raw_text=text,
    )


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _find_price(text: str, pattern: str, flags: int = 0) -> Optional[float]:
    m = re.search(pattern, text, re.IGNORECASE | flags)
    return float(m.group(1)) if m else None


def _guess_trader(text: str) -> str:
    # Intenta sacar el nombre del canal del mensaje si viene como primera línea
    first = text.strip().split('\n')[0].strip()
    if len(first) < 60:
        return first
    return "desconocido"
