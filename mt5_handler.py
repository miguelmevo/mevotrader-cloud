"""
Envía señales al EA MevoTrader vía HTTP.
El EA corre en el VPS con MT5 y es quien ejecuta las órdenes.
"""
import json
import logging
import urllib.request
import urllib.error
from parser import Signal, SignalType

log = logging.getLogger(__name__)


def send_to_ea(signal: Signal, resolved_symbol: str, lots: float, cfg: dict) -> bool:
    """Envía la señal al EA en MT5 vía HTTP."""
    ea_cfg = cfg.get("ea", {})
    host   = ea_cfg.get("host", "127.0.0.1")
    port   = ea_cfg.get("port", 8080)
    url    = f"http://{host}:{port}"

    if signal.type == SignalType.OPEN:
        payload = {
            "action":    "open",
            "symbol":    resolved_symbol,
            "direction": signal.direction,
            "sl":        signal.price if signal.type == SignalType.OPEN else 0.0,
            "tp":        0.0,
        }
    elif signal.type == SignalType.CLOSE:
        payload = {
            "action": "close",
            "symbol": resolved_symbol,
        }
    else:
        return False

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info("EA respondió: %s", resp.read().decode())
            return True
    except urllib.error.URLError as e:
        log.error("No se pudo conectar al EA en %s: %s", url, e)
        return False


def calculate_lots_display(cfg: dict) -> str:
    """Devuelve string descriptivo del lotaje para mostrar en confirmación."""
    trading = cfg.get("trading", {})
    mode    = trading.get("lot_mode", "fixed")
    if mode == "fixed":
        return f"{trading.get('lot_fixed', 0.10)} lots (fijo)"
    else:
        return f"{trading.get('lot_percent', 1.0)}% del balance"
