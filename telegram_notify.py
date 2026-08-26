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

"""
telegram_notify.py

Живі сповіщення в Telegram про хід prom_woo_sync.py:
- одне повідомлення редагується по ходу синхронізації (прогрес),
- помилки летять окремими повідомленнями одразу (щоб не загубились
  при перезаписі прогресу),
- в кінці те саме повідомлення-прогрес перетворюється на підсумок.

Налаштування — через .env (poруч зі скриптом синхронізації):
    TELEGRAM_BOT_TOKEN=123456789:AAH...
    TELEGRAM_CHAT_ID=123456789

Використання (мінімальний приклад):

    from telegram_notify import TelegramNotifier

    notifier = TelegramNotifier()
    notifier.start(total=len(items), label="Синхронізація Olibra -> WooCommerce")

    added = updated = errors = 0
    for i, item in enumerate(items, 1):
        try:
            ... # ваша логіка обробки товару
            added += 1  # або updated += 1
        except Exception as e:
            errors += 1
            notifier.error(f"Товар {item.get('id')}: {e}")

        notifier.progress(processed=i, added=added, updated=updated, errors=errors)

    notifier.finish(added=added, updated=updated, errors=errors)
"""

from __future__ import annotations

import os
import time
import logging
import requests

logger = logging.getLogger("telegram_notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Мінімальний інтервал між редагуваннями "живого" повідомлення (сек),
# щоб не впертись у ліміти Telegram API (не більше ~1 запиту/сек на чат).
MIN_EDIT_INTERVAL = 3.0


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, enabled: bool | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        # Якщо токен/chat_id не задані — сповіщення тихо вимикаються,
        # а не валять синхронізацію помилкою.
        self.enabled = enabled if enabled is not None else bool(self.token and self.chat_id)

        self._message_id = None
        self._label = ""
        self._total = 0
        self._started_at = None
        self._last_edit_ts = 0.0

        if not self.enabled:
            logger.warning("TelegramNotifier вимкнено: не задані TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    # -------------------- низькорівневі виклики API --------------------

    def _call(self, method: str, payload: dict) -> dict | None:
        if not self.enabled:
            return None
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Telegram API помилка (%s): %s", method, data)
            return data
        except Exception as e:
            # Мережева проблема з Telegram не повинна ронити синхронізацію
            logger.warning("Не вдалось звернутись до Telegram API (%s): %s", method, e)
            return None

    def _send(self, text: str) -> int | None:
        data = self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        })
        if data and data.get("ok"):
            return data["result"]["message_id"]
        return None

    def _edit(self, message_id: int, text: str) -> None:
        self._call("editMessageText", {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        })

    # -------------------- публічний інтерфейс --------------------

    def start(self, total: int, label: str = "Синхронізація") -> None:
        """Викликати на самому початку синхронізації (напр. одразу після старту з cron)."""
        self._label = label
        self._total = total
        self._started_at = time.time()

        text = self._render_progress(processed=0, added=0, updated=0, errors=0)
        self._message_id = self._send(text)
        self._last_edit_ts = time.time()

    def progress(self, processed: int, added: int = 0, updated: int = 0, errors: int = 0, force: bool = False) -> None:
        """
        Оновлює "живе" повідомлення. Троттлиться самостійно (MIN_EDIT_INTERVAL),
        тож можна викликати на кожній ітерації циклу без ризику зловити 429 від Telegram.
        force=True — оновити негайно, ігноруючи троттлінг (напр. на останньому товарі).
        """
        if self._message_id is None:
            return
        now = time.time()
        is_last = self._total and processed >= self._total
        if not force and not is_last and (now - self._last_edit_ts) < MIN_EDIT_INTERVAL:
            return
        text = self._render_progress(processed, added, updated, errors)
        self._edit(self._message_id, text)
        self._last_edit_ts = now

    def error(self, text: str) -> None:
        """Помилка/збій — окреме повідомлення одразу, щоб не загубилось при редагуванні прогресу."""
        self._send(f"⚠️ <b>{self._escape(self._label or 'Синхронізація')}</b>\n{self._escape(text)}")

    def finish(self, added: int = 0, updated: int = 0, errors: int = 0, extra_note: str = "") -> None:
        """Викликати в самому кінці — перетворює прогрес-повідомлення на фінальний підсумок."""
        duration = time.time() - self._started_at if self._started_at else 0
        mins, secs = divmod(int(duration), 60)

        status_icon = "✅" if errors == 0 else "⚠️"
        text = (
            f"{status_icon} <b>{self._escape(self._label or 'Синхронізація')} — завершено</b>\n"
            f"Тривалість: {mins} хв {secs} с\n"
            f"Оброблено: {self._total}\n"
            f"Додано: {added}\n"
            f"Оновлено: {updated}\n"
            f"Помилок: {errors}"
        )
        if extra_note:
            text += f"\n\n{self._escape(extra_note)}"

        if self._message_id is not None:
            self._edit(self._message_id, text)
        else:
            self._send(text)

    def fatal(self, text: str) -> None:
        """Критичний збій (скрипт впав, фід недоступний тощо) — окреме гучне повідомлення."""
        self._send(f"🔴 <b>{self._escape(self._label or 'Синхронізація')} — КРИТИЧНА ПОМИЛКА</b>\n{self._escape(text)}")

    # -------------------- допоміжне --------------------

    def _render_progress(self, processed: int, added: int, updated: int, errors: int) -> str:
        total_part = f"/{self._total}" if self._total else ""
        bar = self._render_bar(processed, self._total)
        lines = [
            f"⏳ <b>{self._escape(self._label)}</b>",
            f"{bar} {processed}{total_part}",
            f"Додано: {added} · Оновлено: {updated} · Помилок: {errors}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _render_bar(processed: int, total: int, width: int = 12) -> str:
        if not total:
            return ""
        filled = int(width * min(processed, total) / total)
        return "▓" * filled + "░" * (width - filled)

    @staticmethod
    def _escape(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
