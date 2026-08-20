#!/usr/bin/env python3
"""
telegram_commands.py

Перевіряє нові повідомлення в Telegram і виконує команди:
    /sync    - примусово запустити синхронізацію (якщо вона вже не виконується)
    /status  - показати, чи виконується синхронізація зараз

Розраховано на запуск короткими інтервалами (напр. кожну 1 хвилину) через
Windows Task Scheduler - так само, як prom_woo_sync.py вже запускається за
розкладом. Не тримає постійного фонового процесу/служби.

Слухає повідомлення ЛИШЕ від чату з TELEGRAM_CHAT_ID (з prom_woo_sync.env) -
будь-хто інший ігнорується, навіть якщо напише боту.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(path=None):
        p = Path(path) if path else None
        if p is None or not p.exists():
            return False
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        return True

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / "prom_woo_sync.env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYNC_SCRIPT = SCRIPT_DIR / "prom_woo_sync.py"
OFFSET_FILE = SCRIPT_DIR / "telegram_offset.json"
LOCK_FILE = SCRIPT_DIR / "sync.lock"  # той самий lock-файл, що й у prom_woo_sync.py
LOCK_STALE_SECONDS = 2 * 60 * 60      # має збігатись зі значенням у prom_woo_sync.py

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass  # мережева проблема тут не критична - наступна перевірка через хвилину


def get_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def sync_in_progress() -> bool:
    if not LOCK_FILE.exists():
        return False
    age = time.time() - LOCK_FILE.stat().st_mtime
    return age < LOCK_STALE_SECONDS


def start_sync() -> None:
    if sync_in_progress():
        send("⏳ Синхронізація вже виконується — зачекайте на завершення поточного запуску.")
        return

    send("🚀 Запускаю синхронізацію вручну (команда з Telegram)...")

    # DETACHED_PROCESS - щоб prom_woo_sync.py продовжив працювати незалежно
    # від цього короткоживучого скрипта (Task Scheduler завершить
    # telegram_commands.py одразу після перевірки, а sync триватиме довше).
    creationflags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, str(SYNC_SCRIPT)],
        cwd=str(SCRIPT_DIR),
        creationflags=creationflags,
        close_fds=True,
    )


def handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not TELEGRAM_CHAT_ID or chat_id != str(TELEGRAM_CHAT_ID):
        return  # ігноруємо всіх, крім власника TELEGRAM_CHAT_ID

    text = (msg.get("text") or "").strip().lower()

    if text in ("/sync", "/sync_now", "/синхронізація"):
        start_sync()
    elif text == "/status":
        if sync_in_progress():
            send("⏳ Синхронізація зараз виконується.")
        else:
            send("✅ Зараз синхронізація не виконується.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані в prom_woo_sync.env — вихід.")
        return

    offset = get_offset()
    try:
        resp = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"Не вдалось звернутись до Telegram API: {e}")
        return

    if not data.get("ok"):
        print(f"Telegram API повернув помилку: {data}")
        return

    updates = data.get("result", [])
    for update in updates:
        handle_update(update)
        offset = update["update_id"] + 1

    if updates:
        save_offset(offset)


if __name__ == "__main__":
    main()
