#!/usr/bin/env python3
"""
Синхронізація товарів з Prom.ua (Google Merchant XML фід партнера) у WooCommerce.

Забирає товари з публічного XML-фіда, застосовує націнку, і створює/оновлює
відповідні товари у твоєму WooCommerce магазині (за SKU = g:id з фіда),
включно з побудовою дерева категорій.

Запуск вручну:      python prom_woo_sync.py
Запуск через cron:  0 */3 * * * /usr/bin/python3 /path/to/prom_woo_sync.py >> /var/log/prom_sync.log 2>&1
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    # Простий замінник load_dotenv, щоб скрипт міг працювати без залежності
    def load_dotenv(path=None):
        """Просте завантаження .env-файлу в os.environ.
        Підтримує лише ключ=значення та пропускає коментарі/порожні рядки.
        """
        p = Path(path) if path else None
        if p is None or not p.exists():
            return False
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
        return True

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================
# Чутливі дані (URL магазину, ключі API) НЕ зберігаються тут.
# Вони читаються з файлу prom_woo_sync.env, що лежить поруч зі скриптом.
# Дивись приклад: prom_woo_sync.env.example
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / "prom_woo_sync.env"

if not ENV_PATH.exists():
    sys.exit(
        f"Не знайдено файл {ENV_PATH}\n"
        f"Скопіюй prom_woo_sync.env.example у prom_woo_sync.env і заповни своїми даними."
    )

load_dotenv(ENV_PATH)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"У файлі prom_woo_sync.env відсутнє обов'язкове значення: {name}")
    return value


# Посилання на фід партнера
FEED_URL = require_env("FEED_URL")

# Дані твого WooCommerce магазину
WC_URL = require_env("WC_URL").rstrip("/")
WC_CONSUMER_KEY = require_env("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = require_env("WC_CONSUMER_SECRET")

# Твоя націнка (0.20 = +20% до ціни партнера)
MARKUP = float(os.environ.get("MARKUP", "0.20"))

# Префікс для SKU у твоєму магазині, щоб уникнути конфліктів
# з іншими товарами (наприклад "OLB-47718573")
SKU_PREFIX = os.environ.get("SKU_PREFIX", "OLB-")

# Якщо True — товари, яких більше немає у фіді партнера, будуть
# автоматично переведені у статус "draft" (приховані) у твоєму магазині.
DEACTIVATE_MISSING = os.environ.get("DEACTIVATE_MISSING", "true").lower() == "true"

# Розмір сторінки при запитах до WooCommerce (макс. 100)
WC_PER_PAGE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("prom_woo_sync")

NS = {"g": "http://base.google.com/ns/1.0"}


def _request_with_retry(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """
    Обгортка над requests з повторними спробами при мережевих обривах
    (наприклад, ноутбук заснув / втратив Wi-Fi на секунду / сервер моргнув).
    НЕ повторює спроби при звичайних HTTP-помилках (400, 404 тощо) —
    тільки при справжніх мережевих збоях, де відповіді взагалі не було.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            wait = 5 * attempt
            log.warning(
                "Мережева помилка (спроба %d/%d): %s. Повтор через %d сек...",
                attempt, max_retries, exc, wait,
            )
            time.sleep(wait)
    raise last_exc


# ============================================================
# МОДЕЛЬ ТОВАРУ
# ============================================================

@dataclass
class FeedProduct:
    source_id: str
    title: str
    description: str
    price: float
    in_stock: bool
    image: str
    extra_images: list = field(default_factory=list)
    category_path: str = ""
    brand: str = ""
    attributes: dict = field(default_factory=dict)

    @property
    def sku(self) -> str:
        return f"{SKU_PREFIX}{self.source_id}"

    @property
    def sale_price(self) -> float:
        return round(self.price * (1 + MARKUP), 2)


# ============================================================
# КРОК 1. ПАРСИНГ ФІДА ПАРТНЕРА
# ============================================================

def parse_price(raw: str) -> float:
    """'31.00 UAH' -> 31.0"""
    match = re.search(r"[\d.]+", raw)
    return float(match.group()) if match else 0.0


def fetch_feed_products() -> list[FeedProduct]:
    log.info("Завантажую фід партнера: %s", FEED_URL)
    resp = _request_with_retry("GET", FEED_URL, timeout=60)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    products = []

    for item in root.iter("item"):
        def g(tag, default=""):
            el = item.find(f"g:{tag}", NS)
            return el.text.strip() if el is not None and el.text else default

        images = [el.text for el in item.findall("g:additional_image_link", NS) if el.text]

        attributes = {}
        for detail in item.findall("g:product_detail", NS):
            name_el = detail.find("g:attribute_name", NS)
            value_el = detail.find("g:attribute_value", NS)
            if name_el is not None and value_el is not None:
                attributes[name_el.text.strip()] = (value_el.text or "").strip()

        products.append(FeedProduct(
            source_id=g("id"),
            title=g("title"),
            description=g("description"),
            price=parse_price(g("price", "0")),
            in_stock=(g("availability") == "in stock"),
            image=g("image_link"),
            extra_images=images,
            category_path=g("product_type"),
            brand=g("brand"),
            attributes=attributes,
        ))

    log.info("У фіді знайдено %d товарів", len(products))
    return products


# ============================================================
# КРОК 2. РОБОТА З WOOCOMMERCE REST API
# ============================================================

class WooClient:
    def __init__(self, url: str, key: str, secret: str):
        self.base = f"{url}/wp-json/wc/v3"
        self.auth = (key, secret)

    def get_all_products_by_sku(self) -> dict:
        """
        Повертає {sku: product_id} для всіх товарів з нашим префіксом.

        Важливо: параметр ?search= у WooCommerce REST API робить повнотекстовий
        пошук по назві/опису, а НЕ по SKU — покладатись на нього для пошуку за
        SKU-префіксом ненадійно (продукти "губляться", і скрипт намагається
        створити дублікат із тим самим SKU, що WooCommerce відхиляє з 400).
        Тому тут просто перебираємо всі товари магазину і фільтруємо на своєму боці.
        """
        result = {}
        page = 1
        while True:
            resp = _request_with_retry(
                "GET", f"{self.base}/products",
                auth=self.auth,
                params={"per_page": WC_PER_PAGE, "page": page},
                timeout=60,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for p in batch:
                sku = p.get("sku", "")
                if sku.startswith(SKU_PREFIX):
                    result[sku] = p["id"]
            page += 1
        return result

    def create_product(self, payload: dict) -> int:
        resp = _request_with_retry("POST", f"{self.base}/products", auth=self.auth, json=payload, timeout=60)
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce: %s", resp.text[:500])
        resp.raise_for_status()
        return resp.json()["id"]

    def update_product(self, product_id: int, payload: dict):
        resp = _request_with_retry("PUT", f"{self.base}/products/{product_id}", auth=self.auth, json=payload, timeout=60)
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce: %s", resp.text[:500])
        resp.raise_for_status()

    def set_status(self, product_id: int, status: str):
        self.update_product(product_id, {"status": status})

    # ------------------------------------------------------------
    # Категорії
    # ------------------------------------------------------------

    def get_all_categories(self) -> dict:
        """Повертає {(parent_id, name_lower): id} для всіх категорій, що вже є."""
        result = {}
        page = 1
        while True:
            resp = _request_with_retry(
                "GET", f"{self.base}/products/categories",
                auth=self.auth,
                params={"per_page": WC_PER_PAGE, "page": page},
                timeout=60,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for c in batch:
                result[(c["parent"], c["name"].strip().lower())] = c["id"]
            page += 1
        return result

    def create_category(self, name: str, parent_id: int) -> int:
        resp = _request_with_retry(
            "POST", f"{self.base}/products/categories",
            auth=self.auth,
            json={"name": name, "parent": parent_id},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["id"]


class CategoryTree:
    """
    Будує/кешує дерево категорій WooCommerce з рядка типу
    'Електроніка > Телефони > Смартфони'.

    Категорії, що вже існують у магазині, підхоплюються один раз на старті
    (щоб не створювати дублікати при повторних запусках), нові —
    створюються по ходу і додаються в той самий кеш.
    """

    def __init__(self, wc: "WooClient"):
        self.wc = wc
        self.cache = wc.get_all_categories()  # {(parent_id, name_lower): id}

    def resolve(self, category_path: str) -> Optional[int]:
        path = [part.strip() for part in category_path.split(">") if part.strip()]
        # Prom іноді віддає в product_type просто число (ID власної категорії) —
        # це не назва, і саме воно давало "цифри замість назв" у старому синку.
        # Такі сегменти пропускаємо.
        path = [part for part in path if not part.isdigit()]

        if not path:
            return None

        parent_id = 0
        leaf_id = None
        for name in path:
            key = (parent_id, name.lower())
            if key in self.cache:
                leaf_id = self.cache[key]
            else:
                leaf_id = self.wc.create_category(name, parent_id)
                self.cache[key] = leaf_id
                log.info("Створено категорію '%s' (батько ID=%d)", name, parent_id)
            parent_id = leaf_id

        return leaf_id


def build_payload(p: FeedProduct, category_id: Optional[int] = None) -> dict:
    images = [{"src": p.image}] if p.image else []
    images += [{"src": url} for url in p.extra_images]

    payload = {
        "name": p.title,
        "sku": p.sku,
        "regular_price": str(p.sale_price),
        "description": p.description,
        "short_description": p.description[:300],
        "manage_stock": True,
        "stock_quantity": 10 if p.in_stock else 0,  # фід не завжди дає точний залишок
        "stock_status": "instock" if p.in_stock else "outofstock",
        "status": "publish",
        "images": images,
    }

    if p.attributes:
        payload["attributes"] = [
            {"name": name, "options": [value], "visible": True}
            for name, value in list(p.attributes.items())[:10]  # ліміт, щоб не роздувати запит
        ]

    if category_id is not None:
        payload["categories"] = [{"id": category_id}]

    return payload


# ============================================================
# КРОК 3. ГОЛОВНА ЛОГІКА СИНХРОНІЗАЦІЇ
# ============================================================

def sync():
    feed_products = fetch_feed_products()
    wc = WooClient(WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET)

    log.info("Отримую список товарів, які вже є у WooCommerce...")
    existing = wc.get_all_products_by_sku()
    log.info("У WooCommerce вже є %d товарів з префіксом '%s'", len(existing), SKU_PREFIX)

    log.info("Завантажую/готую дерево категорій...")
    categories = CategoryTree(wc)

    created, updated, failed = 0, 0, 0
    seen_skus = set()
    missing_skus = set()

    for p in feed_products:
        seen_skus.add(p.sku)
        category_id = categories.resolve(p.category_path) if p.category_path else None
        payload = build_payload(p, category_id)
        try:
            if p.sku in existing:
                wc.update_product(existing[p.sku], payload)
                updated += 1
            else:
                wc.create_product(payload)
                created += 1
        except requests.HTTPError as e:
            failed += 1
            log.error("Помилка для товару %s (%s): %s", p.sku, p.title, e)

    if DEACTIVATE_MISSING:
        missing_skus = set(existing.keys()) - seen_skus
        for sku in missing_skus:
            try:
                wc.set_status(existing[sku], "draft")
                log.info("Товар %s відсутній у фіді — переведено в чернетки", sku)
            except requests.HTTPError as e:
                log.error("Не вдалося приховати %s: %s", sku, e)

    log.info(
        "Готово. Створено: %d, оновлено: %d, помилок: %d, приховано: %d",
        created, updated, failed, len(missing_skus) if DEACTIVATE_MISSING else 0,
    )


if __name__ == "__main__":
    try:
        sync()
    except Exception:
        log.exception("Синхронізація завершилась з критичною помилкою")
        sys.exit(1)