"""
Ejecuta este script UNA VEZ para ver todos tus canales de Telegram
y encontrar el ID del canal de LumyArena.
"""
import asyncio
import json
from pathlib import Path
from telethon import TelegramClient

CONFIG_PATH = Path(__file__).parent / "config.json"

async def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    client = TelegramClient(
        "session_lumy",
        int(cfg["telegram"]["api_id"]),
        cfg["telegram"]["api_hash"],
    )

    await client.start()
    print("\n=== TUS CANALES Y GRUPOS DE TELEGRAM ===\n")

    async for dialog in client.iter_dialogs():
        print(f"ID: {dialog.id}  |  Nombre: {dialog.name}")

    print("\n=== Copia el ID del canal de LumyArena ===\n")
    await client.disconnect()

asyncio.run(main())
