# Setup — Telegram → MT5 Copy Trading

## 1. Instalar dependencias

```bash
cd ~/Documents/telegram_mt5
pip install -r requirements.txt
```

> MetaTrader5 solo funciona en Windows. En Mac con Wine/Parallels, ejecuta el script desde Windows.

---

## 2. Obtener credenciales de Telegram

### API ID y API Hash (para leer el canal con tu cuenta)
1. Ve a https://my.telegram.org/apps
2. Crea una app → copia **api_id** y **api_hash**

### Bot Token (para recibir confirmaciones)
1. Habla con @BotFather en Telegram → `/newbot`
2. Copia el token

### Tu Chat ID
1. Habla con @userinfobot → te dice tu chat_id numérico

### ID del canal privado
- Si es un canal, el ID suele ser negativo: `-100XXXXXXXXX`
- Puedes buscarlo con Telethon: el script imprime los chats al conectar la primera vez

---

## 3. Configurar config.json

```json
{
  "telegram": {
    "api_id": "12345678",
    "api_hash": "abcdef1234567890abcdef",
    "source_channel": -1001234567890,
    "bot_token": "123456789:AABBccDDeEFfGgHhIiJj",
    "admin_chat_id": "987654321"
  },
  "mt5": {
    "login": 12345678,
    "password": "tu_password",
    "server": "NombreBroker-Live",
    "symbol_suffix": "",
    "magic_number": 20240101
  },
  "trading": {
    "lot_mode": "fixed",
    "lot_fixed": 0.10,
    "lot_percent": 1.0,
    "confirmation_timeout_seconds": 60,
    "close_requires_confirmation": false
  }
}
```

### Parámetros de lotaje:
- `lot_mode: "fixed"` → usa siempre `lot_fixed` lots
- `lot_mode: "percent"` → calcula lots según `lot_percent`% del balance / margen por lote
- `close_requires_confirmation: false` → los cierres son automáticos (recomendado)

---

## 4. Ejecutar

```bash
python main.py
```

La primera vez, Telethon pedirá tu número de teléfono y código de verificación (sesión guardada en `session_lumy.session`).

---

## Flujo de uso

1. Llega señal de apertura en el canal → recibes notificación en Telegram (bot) + macOS
2. Pulsas **✅ Aprobar** → se abre la orden en MT5
3. Llega señal de cierre → si `close_requires_confirmation: false`, se cierra automáticamente
4. Recibes confirmación por Telegram con el ticket cerrado
