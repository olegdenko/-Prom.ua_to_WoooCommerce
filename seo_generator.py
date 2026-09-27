"""
seo_generator.py

Модуль генерації SEO метаданих (Rank Math) для товарів WooCommerce через OpenAI.

Використання:
  - автономно (вся база):     python3 seo_generator.py --all
  - точково (один товар):     python3 seo_generator.py --product-id 12345
  - як імпорт у sync-скрипт:  from seo_generator import process_single_product

Дотримується тих самих принципів безпеки, що й prom_woo_sync.py:
  - за замовчуванням обробляє лише товари, чий SKU починається на SKU_PREFIX
    (щоб не чіпати вручну додані в магазин товари). Вимикається прапорцем --all-sku.
"""

import os
import sys
import json
import time
import argparse
import logging

import requests
from woocommerce import API
from openai import OpenAI, RateLimitError, APIError

# ---------------------------------------------------------------------------
# Конфігурація — лише з середовища, жодних ключів у коді
# ---------------------------------------------------------------------------
WC_URL = os.environ.get("WC_URL", "https://denko.asuscomm.com")
WC_CONSUMER_KEY = os.environ.get("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.environ.get("WC_CONSUMER_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SKU_PREFIX = os.environ.get("SKU_PREFIX", "OLB-")
OPENAI_MODEL = os.environ.get("SEO_OPENAI_MODEL", "gpt-4o-mini")
REQUEST_DELAY = float(os.environ.get("SEO_REQUEST_DELAY", "1.0"))
MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [seo_generator] %(levelname)s: %(message)s",
)
log = logging.getLogger("seo_generator")


def _require_config():
    missing = [
        name
        for name, val in [
            ("WC_CONSUMER_KEY", WC_CONSUMER_KEY),
            ("WC_CONSUMER_SECRET", WC_CONSUMER_SECRET),
            ("OPENAI_API_KEY", OPENAI_API_KEY),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(f"Задайте у середовищі: {', '.join(missing)}")


_require_config()

wcapi = API(
    url=WC_URL,
    consumer_key=WC_CONSUMER_KEY,
    consumer_secret=WC_CONSUMER_SECRET,
    version="wc/v3",
    timeout=30,
)

client = OpenAI(api_key=OPENAI_API_KEY)

META_KEYS = {
    "title": "rank_math_title",
    "description": "rank_math_description",
    "focus_keyword": "rank_math_focus_keyword",
}


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
    """Генерує SEO title/description/focus_keyword через OpenAI, з ретраями на 429."""
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
        except RateLimitError:
            wait = 2 ** attempt
            log.warning(f"OpenAI 429 (rate limit), спроба {attempt}/{MAX_RETRIES}, чекаю {wait}с")
            time.sleep(wait)
        except APIError as e:
            log.error(f"Помилка OpenAI API: {e}")
            return None
        except (json.JSONDecodeError, KeyError) as e:
            log.error(f"Некоректна відповідь OpenAI (не JSON або бракує полів): {e}")
            return None
    log.error("Вичерпано спроби звернення до OpenAI (rate limit)")
    return None


def update_product_rank_math_seo(product: dict) -> bool:
    """Оновлює мета-поля Rank Math для вже завантаженого товару (dict)."""
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

    try:
        res = wcapi.put(f"products/{p_id}", payload)
    except requests.RequestException as e:
        log.error(f"Мережева помилка при оновленні ID {p_id}: {e}")
        return False

    if res.status_code == 200:
        log.info(f"✅ SEO успішно оновлено для ID {p_id}")
        return True

    log.error(f"❌ Помилка оновлення WooCommerce для ID {p_id}: {res.text}")
    return False


def process_single_product(product_id: int, enforce_sku: bool = True) -> bool:
    """
    Точка входу для інтеграції з prom_woo_sync.py.
    Викликати одразу після успішного create_product()/update_product() для товару.
    Приклад:
        response = woo_client.create_product(product_data)
        if response.status_code == 201:
            new_product = response.json()
            process_single_product(new_product["id"])
    """
    try:
        res = wcapi.get(f"products/{product_id}")
    except requests.RequestException as e:
        log.error(f"Мережева помилка при завантаженні товару ID {product_id}: {e}")
        return False

    if res.status_code != 200:
        log.error(f"Не вдалось завантажити товар ID {product_id}: {res.text}")
        return False

    product = res.json()

    if not _owns_product(product, enforce_sku):
        log.info(f"Пропуск ID {product_id} — SKU не належить SKU_PREFIX={SKU_PREFIX!r}")
        return False

    if _has_seo(product):
        log.info(f"Пропуск ID {product_id} — SEO вже заповнено.")
        return False

    return update_product_rank_math_seo(product)


def process_all_unfilled_products(enforce_sku: bool = True, per_page: int = 50):
    """Масова обробка товарів без SEO (перший повний прогін / ручний запуск --all)."""
    page = 1
    processed = skipped = failed = 0

    while True:
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
            if not _owns_product(product, enforce_sku):
                skipped += 1
                continue
            if _has_seo(product):
                log.info(f"Пропуск ID {product['id']} — SEO вже заповнено.")
                skipped += 1
                continue

            ok = update_product_rank_math_seo(product)
            processed += int(ok)
            failed += int(not ok)
            time.sleep(REQUEST_DELAY)  # пауза, щоб не перевищити RPM OpenAI API

        page += 1

    log.info(f"Готово. Оброблено: {processed}, пропущено: {skipped}, помилок: {failed}")


def _cli():
    parser = argparse.ArgumentParser(description="Генератор Rank Math SEO метаданих через OpenAI")
    parser.add_argument("--all", action="store_true", help="Обробити всю базу товарів без SEO")
    parser.add_argument("--product-id", type=int, help="Обробити один конкретний товар за ID")
    parser.add_argument(
        "--all-sku",
        action="store_true",
        help="Не обмежуватись SKU_PREFIX — обробляти будь-які товари (обережно!)",
    )
    args = parser.parse_args()
    enforce_sku = not args.all_sku

    if args.product_id:
        process_single_product(args.product_id, enforce_sku=enforce_sku)
    elif args.all:
        process_all_unfilled_products(enforce_sku=enforce_sku)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    _cli()
