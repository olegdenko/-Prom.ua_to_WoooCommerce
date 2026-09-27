"""
seo_generator.py

Модуль генерації SEO метаданих (Rank Math) для товарів WooCommerce через OpenAI.

Використання:
  - автономно (вся база):     python3 seo_generator.py --all
  - точково (один товар):     python3 seo_generator.py --product-id 12345
  - перевірка налаштувань:    python3 seo_generator.py --check
  - як імпорт у sync-скрипт:  from seo_generator import process_single_product

ПРИНЦИПИ БЕЗПЕКИ (узгоджено з prom_woo_sync.py):
  - Чутливі дані (WC_URL/ключі, OPENAI_API_KEY) читаються з того самого файлу
    prom_woo_sync.env, що лежить поруч зі скриптами (див. prom_woo_sync.env.example).
    Ніяких ключів у коді, нічого не дублюється в окремому .env-файлі.
  - За замовчуванням обробляються лише товари, чий SKU починається на SKU_PREFIX
    (щоб не чіпати вручну додані в магазин товари). Вимикається --all-sku.
  - Цей модуль НІКОЛИ не кидає виняток назовні з process_single_product() /
    process_all_unfilled_products() — якщо OpenAI недоступний, скінчились кошти
    на рахунку, чи API-ключ невалідний, функції тихо повертають False/логують
    помилку, а не обривають виклик prom_woo_sync.py, який їх імпортує.
  - Якщо OpenAI поверне "insufficient_quota" (кошти/безкоштовний ліміт скінчились)
    або невалідний ключ — генерація одразу вимикається до кінця ПОТОЧНОГО запуску
    процесу (щоб не бити по API 1400 разів поспіль з тим самим результатом, і не
    палити час на невдалі ретраї). Наступний запуск (наступний день) спробує знову.
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import logging
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Завантаження спільного .env файлу (той самий, що й у prom_woo_sync.py)
# Робимо це тут незалежно від того, хто і в якому порядку нас імпортує —
# інакше при імпорті РАНІШЕ за load_dotenv() у прom_woo_sync.py змінні ще
# не будуть у os.environ, і модуль впаде ще до старту синхронізації.
# ---------------------------------------------------------------------------
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
ENV_PATH = SCRIPT_DIR / "prom_woo_sync.env"
load_dotenv(ENV_PATH)  # тихо: якщо файлу немає — просто працюємо з тим, що вже в os.environ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [seo_generator] %(levelname)s: %(message)s",
)
log = logging.getLogger("seo_generator")

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------
WC_URL = os.environ.get("WC_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.environ.get("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.environ.get("WC_CONSUMER_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SKU_PREFIX = os.environ.get("SKU_PREFIX", "OLB-")
OPENAI_MODEL = os.environ.get("SEO_OPENAI_MODEL", "gpt-4o-mini")
REQUEST_DELAY = float(os.environ.get("SEO_REQUEST_DELAY", "1.0"))
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# М'яка ініціалізація: НІЧОГО тут не кидає виняток при імпорті. Якщо чогось
# бракує (немає ключа, не встановлено пакет openai/woocommerce) — модуль
# просто позначає себе вимкненим (SEO_ENABLED=False) і всі публічні функції
# стають безпечними no-op, замість того щоб зупиняти prom_woo_sync.py.
# ---------------------------------------------------------------------------
SEO_ENABLED = True
_DISABLE_REASON: str | None = None


def _startup_disable(reason: str):
    global SEO_ENABLED, _DISABLE_REASON
    SEO_ENABLED = False
    _DISABLE_REASON = reason
    log.warning(f"⚠️ SEO-модуль вимкнено: {reason}")


missing = [
    name for name, val in [
        ("WC_URL", WC_URL),
        ("WC_CONSUMER_KEY", WC_CONSUMER_KEY),
        ("WC_CONSUMER_SECRET", WC_CONSUMER_SECRET),
        ("OPENAI_API_KEY", OPENAI_API_KEY),
    ] if not val
]
if missing:
    _startup_disable(f"не задано в prom_woo_sync.env: {', '.join(missing)}")

wcapi = None
client = None
RateLimitError = AuthenticationError = PermissionDeniedError = APIError = Exception  # заглушки на випадок ImportError

if SEO_ENABLED:
    try:
        from woocommerce import API
        from openai import (
            OpenAI,
            RateLimitError,
            AuthenticationError,
            PermissionDeniedError,
            APIError,
        )

        wcapi = API(
            url=WC_URL,
            consumer_key=WC_CONSUMER_KEY,
            consumer_secret=WC_CONSUMER_SECRET,
            version="wc/v3",
            timeout=30,
        )
        client = OpenAI(api_key=OPENAI_API_KEY)
    except ImportError as e:
        _startup_disable(
            f"не встановлено потрібний пакет ({e}). "
            f"Виконайте: pip install openai woocommerce"
        )
    except Exception as e:
        _startup_disable(f"помилка ініціалізації клієнтів API: {e}")

META_KEYS = {
    "title": "rank_math_title",
    "description": "rank_math_description",
    "focus_keyword": "rank_math_focus_keyword",
}

# ---------------------------------------------------------------------------
# "Запобіжник" (circuit breaker) на час одного запуску процесу: якщо OpenAI
# каже "скінчились кошти/ліміт" або ключ недійсний — далі навіть не пробуємо,
# щоб не витрачати час на 1400 однакових невдалих спроб.
# ---------------------------------------------------------------------------
_RUNTIME_DISABLED_REASON: str | None = None


def _seo_available() -> bool:
    return SEO_ENABLED and _RUNTIME_DISABLED_REASON is None


def _disable_at_runtime(reason: str):
    global _RUNTIME_DISABLED_REASON
    if _RUNTIME_DISABLED_REASON is None:
        _RUNTIME_DISABLED_REASON = reason
        log.error(f"🔴 SEO-генерація вимкнена до кінця цього запуску: {reason}")


def is_seo_disabled() -> str | None:
    """
    Повертає причину, чому SEO-генерація недоступна прямо зараз (для звіту
    в кінці sync() з боку prom_woo_sync.py), або None, якщо все гаразд.
    """
    if not SEO_ENABLED:
        return _DISABLE_REASON
    return _RUNTIME_DISABLED_REASON


def _is_quota_exhausted(exc: Exception) -> bool:
    """OpenAI повертає той самий RateLimitError (HTTP 429) і для 'занадто
    швидко шлете запити', і для 'скінчились кошти на рахунку' — розрізняємо
    за текстом помилки, бо це різні за змістом ситуації (перше — почекати
    і повторити, друге — сенсу повторювати нема, поки не поповнять баланс)."""
    text = str(exc).lower()
    return "insufficient_quota" in text or "exceeded your current quota" in text


def _has_seo(product: dict) -> bool:
    """Чи товар вже має заповнене фокусне слово Rank Math."""
    meta_list = product.get("meta_data", [])
    return any(
        m.get("key") == META_KEYS["focus_keyword"] and m.get("value")
        for m in meta_list
    )


def _owns_product(product: dict, enforce_sku: bool) -> bool:
    """За принципом prom_woo_sync.py — за замовчуванням чіпаємо лише свої товари."""
    if not enforce_sku:
        return True
    sku = product.get("sku") or ""
    return sku.startswith(SKU_PREFIX)


def generate_seo_metadata(product_title: str, categories: str, short_description: str = ""):
    """Генерує SEO title/description/focus_keyword через OpenAI. Ніколи не кидає
    виняток — повертає None при будь-якій проблемі."""
    if not _seo_available():
        return None

    prompt = f"""
Ти SEO-фахівець інтернет-магазину DENKO.
Згенеруй SEO метадані українською мовою для товару:
- Назва товару: {product_title}
- Категорії: {categories}
- Опис: {short_description}

Вимоги:
1. SEO Title: До 55-60 символів, обов'язково із закінченням " — DENKO". Не використовуй слово "одноразова".
2. Meta Description: До 145-150 символів із закликом до дії (Купуйте в DENKO!).
3. Focus Keyword: Основна ключова фраза нижнім регістром.

Відповідай СУВОРО у форматі JSON:
{{
    "title": "...",
    "description": "...",
    "focus_keyword": "..."
}}
"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            return json.loads(response.choices[0].message.content)

        except RateLimitError as e:
            if _is_quota_exhausted(e):
                _disable_at_runtime(f"вичерпано ліміт/кошти OpenAI ({e})")
                return None
            wait = 2 ** attempt
            log.warning(f"OpenAI 429 (забагато запитів/хв), спроба {attempt}/{MAX_RETRIES}, чекаю {wait}с")
            time.sleep(wait)

        except AuthenticationError as e:
            _disable_at_runtime(f"недійсний OPENAI_API_KEY ({e})")
            return None

        except PermissionDeniedError as e:
            _disable_at_runtime(f"доступ заборонено — перевірте ключ/організацію OpenAI ({e})")
            return None

        except APIError as e:
            log.error(f"Помилка OpenAI API: {e}")
            return None

        except (json.JSONDecodeError, KeyError) as e:
            log.error(f"Некоректна відповідь OpenAI (не JSON або бракує полів): {e}")
            return None

        except Exception as e:
            # Останній рубіж захисту: що б несподіване не сталося (обрив
            # з'єднання всередині SDK, зміна формату відповіді тощо) —
            # ніколи не даємо цьому впасти нагору, в prom_woo_sync.py.
            log.exception(f"Неочікувана помилка при зверненні до OpenAI: {e}")
            return None

    log.error("Вичерпано спроби звернення до OpenAI (rate limit)")
    return None


def update_product_rank_math_seo(product: dict) -> bool:
    """Оновлює мета-поля Rank Math для вже завантаженого товару (dict).
    Ніколи не кидає виняток назовні."""
    try:
        p_id = product["id"]
        p_title = product["name"]
        categories = ", ".join(c["name"] for c in product.get("categories", []))
        short_desc = product.get("short_description") or product.get("description") or ""

        log.info(f"Генерація SEO для товару ID {p_id}: {p_title}")
        seo_data = generate_seo_metadata(p_title, categories, short_desc)
        if not seo_data:
            return False

        payload = {
            "meta_data": [
                {"key": META_KEYS["title"], "value": seo_data["title"]},
                {"key": META_KEYS["description"], "value": seo_data["description"]},
                {"key": META_KEYS["focus_keyword"], "value": seo_data["focus_keyword"]},
            ]
        }

        res = wcapi.put(f"products/{p_id}", payload)
        if res.status_code == 200:
            log.info(f"✅ SEO успішно оновлено для ID {p_id}")
            return True

        log.error(f"❌ Помилка оновлення WooCommerce для ID {p_id}: {res.text}")
        return False

    except requests.RequestException as e:
        log.error(f"Мережева помилка при оновленні SEO товару: {e}")
        return False
    except Exception as e:
        log.exception(f"Неочікувана помилка при оновленні SEO товару: {e}")
        return False


def process_single_product(product_id: int, enforce_sku: bool = True, overwrite: bool = False) -> bool:
    """
    Точка входу для інтеграції з prom_woo_sync.py. Абсолютно безпечна для
    виклику з основного циклу синхронізації — при БУДЬ-ЯКІЙ проблемі (немає
    ключа, немає інтернету, скінчились кошти OpenAI, пакет не встановлено)
    просто повертає False і пише в лог, ніколи не кидає виняток.

    overwrite=False (за замовчуванням) — товар з уже заповненим SEO (в т.ч.
        вручну адміністратором) пропускається, ніяких змін.
    overwrite=True — SEO перегенерується й перезаписується, навіть якщо вже
        було заповнено вручну. Використовувати свідомо (наприклад, окрема
        команда в telegram_commands.py на кшталт /seo_refresh <product_id>),
        а не автоматично при кожному sync().

    Приклад:
        response = wc.create_product(payload)
        process_single_product(response["id"])
    """
    if not _seo_available():
        log.info(f"Пропуск ID {product_id} — SEO-модуль вимкнено ({is_seo_disabled()})")
        return False

    try:
        res = wcapi.get(f"products/{product_id}")
    except requests.RequestException as e:
        log.error(f"Мережева помилка при завантаженні товару ID {product_id}: {e}")
        return False
    except Exception as e:
        log.exception(f"Неочікувана помилка при завантаженні товару ID {product_id}: {e}")
        return False

    if res.status_code != 200:
        log.error(f"Не вдалось завантажити товар ID {product_id}: {res.text}")
        return False

    product = res.json()

    if not _owns_product(product, enforce_sku):
        log.info(f"Пропуск ID {product_id} — SKU не належить SKU_PREFIX={SKU_PREFIX!r}")
        return False

    if _has_seo(product) and not overwrite:
        log.info(f"Пропуск ID {product_id} — SEO вже заповнено (в т.ч. можливо вручну).")
        return False

    return update_product_rank_math_seo(product)


def process_all_unfilled_products(enforce_sku: bool = True, per_page: int = 50, overwrite: bool = False):
    """
    Масова обробка товарів (перший повний прогін / ручний запуск --all).
    overwrite=False — обробляються тільки товари БЕЗ SEO (звичайний режим).
    overwrite=True  — перегенеруються ВСІ товари, що підпадають під SKU_PREFIX,
        включно з тими, де SEO вже виставлено вручну.
    """
    if not _seo_available():
        log.error(f"SEO-модуль вимкнено, масову обробку не запущено: {is_seo_disabled()}")
        return

    page = 1
    processed = skipped = failed = 0

    while True:
        if not _seo_available():
            log.error(f"Зупиняюсь: {is_seo_disabled()}")
            break

        log.info(f"Завантаження сторінки {page}...")
        try:
            res = wcapi.get(
                "products",
                params={"per_page": per_page, "page": page, "status": "any"},
            )
        except requests.RequestException as e:
            log.error(f"Мережева помилка при завантаженні сторінки {page}: {e}")
            break

        if res.status_code != 200:
            log.error(f"Помилка WooCommerce API на сторінці {page}: {res.text}")
            break

        products = res.json()
        if not products:
            break  # товари закінчилися

        for product in products:
            if not _seo_available():
                log.error(f"Зупиняюсь посеред сторінки {page}: {is_seo_disabled()}")
                break

            if not _owns_product(product, enforce_sku):
                skipped += 1
                continue
            if _has_seo(product) and not overwrite:
                log.info(f"Пропуск ID {product['id']} — SEO вже заповнено.")
                skipped += 1
                continue

            ok = update_product_rank_math_seo(product)
            processed += int(ok)
            failed += int(not ok)
            time.sleep(REQUEST_DELAY)  # пауза, щоб не перевищити RPM OpenAI API

        page += 1

    log.info(f"Готово. Оброблено: {processed}, пропущено: {skipped}, помилок: {failed}")
    if is_seo_disabled():
        log.error(f"Прогін завершено достроково: {is_seo_disabled()}")


def _cli():
    parser = argparse.ArgumentParser(description="Генератор Rank Math SEO метаданих через OpenAI")
    parser.add_argument("--all", action="store_true", help="Обробити всю базу товарів без SEO")
    parser.add_argument("--product-id", type=int, help="Обробити один конкретний товар за ID")
    parser.add_argument(
        "--all-sku", action="store_true",
        help="Не обмежуватись SKU_PREFIX — обробляти будь-які товари (обережно!)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Перезаписати SEO навіть там, де воно вже є (в т.ч. виставлене вручну)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Тільки перевірити налаштування (ключі, пакети) і вийти",
    )
    args = parser.parse_args()

    if args.check:
        if SEO_ENABLED:
            print(f"✅ SEO-модуль готовий до роботи. Модель: {OPENAI_MODEL}, WC_URL: {WC_URL}")
            sys.exit(0)
        else:
            print(f"❌ SEO-модуль НЕ готовий: {_DISABLE_REASON}")
            sys.exit(1)

    if not SEO_ENABLED:
        print(f"❌ SEO-модуль вимкнено: {_DISABLE_REASON}\nЗапустіть 'python3 seo_generator.py --check' для деталей.")
        sys.exit(1)

    enforce_sku = not args.all_sku

    if args.product_id:
        process_single_product(args.product_id, enforce_sku=enforce_sku, overwrite=args.overwrite)
    elif args.all:
        process_all_unfilled_products(enforce_sku=enforce_sku, overwrite=args.overwrite)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    _cli()
