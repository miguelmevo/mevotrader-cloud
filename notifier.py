import subprocess
import logging
import platform
from parser import Signal, SignalType

log = logging.getLogger(__name__)


def desktop_notification(title: str, body: str, subtitle: str = ""):
    """Notificación nativa — macOS o Windows."""
    if platform.system() == "Darwin":
        script = f'display notification "{body}" with title "{title}"'
        if subtitle:
            script += f' subtitle "{subtitle}"'
        try:
            subprocess.run(["osascript", "-e", script], check=True)
        except Exception as e:
            log.warning("macOS notification falló: %s", e)

    elif platform.system() == "Windows":
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, f"{subtitle}\n{body}", duration=10, threaded=True)
        except Exception as e:
            log.warning("Windows notification falló: %s", e)


def format_open_message(signal: Signal, resolved_symbol: str, lots: float, channel_name: str = "") -> str:
    sl_str = f"\nSL: `{signal.sl}`" if signal.sl else ""
    tp_str = f"\nTP: `{signal.tp}`" if signal.tp else ""
    ch_str = f"\n📢 Instrumento: *{channel_name}*" if channel_name and channel_name != "—" else ""
    return (
        f"📡 *NUEVA ALERTA — {signal.trader}*\n\n"
        f"*{resolved_symbol}* — `{signal.direction}`\n"
        f"Precio: `{signal.price}`"
        f"{sl_str}{tp_str}"
        f"{ch_str}\n\n"
        f"¿Abrir esta operación?"
    )


def format_close_message(signal: Signal, resolved_symbol: str, channel_name: str = "") -> str:
    pips_str = f"{signal.pips:+.1f} pips" if signal.pips is not None else "—"
    profit_str = signal.profit or "—"
    ch_str = f"\n📢 Instrumento: *{channel_name}*" if channel_name and channel_name != "—" else ""
    return (
        f"🔴 *CIERRE — {signal.trader}*\n\n"
        f"*{resolved_symbol}* — `{signal.direction}`\n"
        f"Apertura: `{signal.open_price}` → Cierre: `{signal.close_price}`\n"
        f"Resultado alerta: `{pips_str}` | `{profit_str}`"
        f"{ch_str}\n\n"
        f"¿Cerrar tu posición?"
    )


def notify_desktop_open(signal: Signal, resolved_symbol: str, lots: float):
    desktop_notification(
        title=f"Alerta ABRIR — {resolved_symbol}",
        body=f"{signal.direction} {lots} lots @ {signal.price}",
        subtitle=f"Trader: {signal.trader}",
    )


def notify_desktop_close(signal: Signal, resolved_symbol: str):
    desktop_notification(
        title=f"Alerta CERRAR — {resolved_symbol}",
        body=f"{signal.direction} | {signal.pips:+.1f} pips" if signal.pips else signal.direction,
        subtitle=f"Trader: {signal.trader}",
    )
