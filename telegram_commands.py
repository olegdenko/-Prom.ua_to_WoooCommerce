# Copyright 2026 Oleh Demydenko
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python3
"""
telegram_commands.py

Перевіряє нові повідомлення в Telegram і виконує команди:
    /sync    - примусово запустити синхронізацію (якщо вона вже не виконується)
    /scrape  - примусово запустити скрапер категорій партнера (10-20+ хв,
               якщо він вже не виконується і не виконується /sync)
    /status  - показати, чи виконується синхронізація і/або скрапер зараз

Розраховано на запуск короткими інтервалами (напр. кожну 1 хвилину) через
Windows Task Scheduler - так само, як prom_woo_sync.py вже запускається за
розкладом. Не тримає постійного фонового процесу/служби.

Слухає повідомлення ЛИШЕ від чату з TELEGRAM_CHAT_ID (з prom_woo_sync.env) -
будь-хто інший ігнорується, навіть якщо напише боту.
"""

from __future__ import annotations

import json
import os
import shlex
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
SCRAPE_SCRIPT = SCRIPT_DIR / "olibra_categories_scraper.py"
OFFSET_FILE = SCRIPT_DIR / "telegram_offset.json"

LOCK_FILE = SCRIPT_DIR / "sync.lock"  # той самий lock-файл, що й у prom_woo_sync.py
LOCK_STALE_SECONDS = 2 * 60 * 60      # має збігатись зі значенням у prom_woo_sync.py

SCRAPE_LOCK_FILE = SCRIPT_DIR / "scrape.lock"  # той самий lock-файл, що й у olibra_categories_scraper.py
SCRAPE_LOCK_STALE_SECONDS = 60 * 60            # має збігатись зі значенням там же

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


def _lock_active(lock_file: Path, stale_seconds: int) -> bool:
    if not lock_file.exists():
        return False
    age = time.time() - lock_file.stat().st_mtime
    return age < stale_seconds


def sync_in_progress() -> bool:
    return _lock_active(LOCK_FILE, LOCK_STALE_SECONDS)


def scrape_in_progress() -> bool:
    return _lock_active(SCRAPE_LOCK_FILE, SCRAPE_LOCK_STALE_SECONDS)


def _run_detached(script: Path, label: str) -> None:
    """Запускає script у фоні (detached, незалежно від цього
    короткоживучого telegram_commands.py) і, на POSIX (Ubuntu, де зараз
    реально живе проєкт), додатково шле в Telegram повідомлення про
    завершення - успіх/помилка і скільки часу зайняло. Це окремий
    detached bash-ланцюжок, тому не потребує, щоб сам telegram_commands.py
    чекав на завершення довгого sync/scrape."""
    if os.name == "nt":
        # Завершальне повідомлення на Windows не реалізовано (bat-еквівалент
        # ланцюжка нижче виглядав би суттєво складніше, а актуальний прод -
        # Ubuntu/cron) - запускаємо як і раніше, без нотифікації про фініш.
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(SCRIPT_DIR),
            creationflags=subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        return

    python_q = shlex.quote(sys.executable)
    script_q = shlex.quote(str(script))
    chat_id_q = shlex.quote(TELEGRAM_CHAT_ID)
    label_q = label.replace('"', "")  # для тексту повідомлення, лапки прибираємо про всяк випадок

    bash_cmd = (
        f"START=$(date +%s); "
        f"{python_q} {script_q}; "
        f"RC=$?; "
        f"ELAPSED=$(( $(date +%s) - START )); "
        f'if [ $RC -eq 0 ]; then MSG="✅ {label_q}: готово за $((ELAPSED/60))хв $((ELAPSED%60))с."; '
        f'else MSG="❌ {label_q}: помилка (код $RC), минуло $((ELAPSED/60))хв $((ELAPSED%60))с. Дивись лог на сервері."; fi; '
        f'curl -s -X POST "https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" '
        f'-d chat_id={chat_id_q} --data-urlencode "text=$MSG" > /dev/null'
    )
    subprocess.Popen(
        ["/bin/bash", "-c", bash_cmd],
        cwd=str(SCRIPT_DIR),
        start_new_session=True,  # POSIX-еквівалент detach - переживе завершення цього процесу
        close_fds=True,
    )


def start_sync() -> None:
    if sync_in_progress():
        send("⏳ Синхронізація вже виконується — зачекайте на завершення поточного запуску.")
        return
    if scrape_in_progress():
        send("⏳ Зараз виконується скрапер категорій (/scrape) — синхронізація читає його файли, "
             "тому зачекайте на завершення скрапера і повторіть /sync.")
        return

    send("🚀 Запускаю синхронізацію вручну (команда з Telegram)...")
    _run_detached(SYNC_SCRIPT, "Синхронізація")


def start_scrape() -> None:
    if scrape_in_progress():
        send("⏳ Скрапер категорій вже виконується — зачекайте на завершення поточного запуску.")
        return
    if sync_in_progress():
        send("⏳ Зараз виконується синхронізація (/sync) — скрапер не можна запускати паралельно "
             "(обидва читають/пишуть pending_orphans.json), зачекайте на завершення і повторіть /scrape.")
        return

    send("🚀 Запускаю скрапер категорій партнера вручну (команда з Telegram)... "
         "Це довго (10-20+ хв), напишу коли завершиться.")
    _run_detached(SCRAPE_SCRIPT, "Скрапер категорій")


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
    elif text in ("/scrape", "/scrape_now", "/скрапер", "/категорії"):
        start_scrape()
    elif text == "/status":
        sync_line = "⏳ Синхронізація зараз виконується." if sync_in_progress() else "✅ Синхронізація не виконується."
        scrape_line = "⏳ Скрапер категорій зараз виконується." if scrape_in_progress() else "✅ Скрапер категорій не виконується."
        send(f"{sync_line}\n{scrape_line}")


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
