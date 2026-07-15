"""
Ejecuta este script UNA VEZ en tu Mac para generar la sesión de Telethon.
Usa StringSession (cadena compacta) en lugar de archivo SQLite.

Uso:
  1. python3 auth_session.py
  2. Ingresa tu número y el código de Telegram
  3. Copia la línea "TG_STRING_SESSION=..." y agrégala en Railway → Variables
"""
import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_creds():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg["telegram"]["api_id"], cfg["telegram"]["api_hash"]

async def main():
    api_id, api_hash = load_creds()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()

    me = await client.get_me()
    session_string = client.session.save()
    print(f"\n✅ Autenticado como: {me.first_name} (@{me.username})")
    await client.disconnect()

    print("\n" + "="*60)
    print("Copia esta variable en Railway → Variables:")
    print("="*60)
    print(f"TG_STRING_SESSION={session_string}")
    print("="*60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
