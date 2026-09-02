#!/usr/bin/env python3
"""
Синхронізація товарів з Prom.ua (Google Merchant XML фід партнера) у WooCommerce.

Забирає товари з публічного XML-фіда, застосовує націнку, і створює/оновлює
відповідні товари у твоєму WooCommerce магазині (за SKU = g:id з фіда),
включно з побудовою дерева категорій.

ДЕДУПЛІКАЦІЯ МЕДІА (важливо): WooCommerce REST API, отримавши для товару чи
категорії {"image": {"src": url}}, ЩОРАЗУ завантажує зображення заново і
створює НОВЕ вкладення в медіатеці - навіть якщо це той самий url, що й у
попередньому запуску. Оскільки цей скрипт оновлює товари кожні кілька годин
і щоразу заново шле src - за тижні це дає десятки тисяч дублікатів медіа.
Тому тепер ведеться локальний кеш url -> attachment_id (media_cache.json):
якщо url вже завантажувався раніше - шлемо {"id": id} (WooCommerce просто
прив'язує вже наявне вкладення, нічого не перезавантажуючи); якщо це
справді новий url - шлемо {"src": url} як і раніше, а отриманий у відповіді
attachment_id одразу кешуємо на майбутнє.

Це саме стосується і фото категорій: раніше воно виставлялось ТІЛЬКИ при
створенні нової категорії - якщо категорія вже існувала в WooCommerce (навіть
без фото, наприклад після ручного видалення вкладень), фото їй ніколи більше
не пробувало виставитись. Тепер CategoryTree.resolve_from_chain() один раз
за прогон донастановлює фото будь-якій існуючій категорії, у якої його немає.

Запуск вручну:      python prom_woo_sync.py
Запуск через cron:  0 */3 * * * /usr/bin/python3 /path/to/prom_woo_sync.py >> /var/log/prom_sync.log 2>&1
"""

from __future__ import annotations

import json
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

from telegram_notify import TelegramNotifier

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

# ============================================================
# ТРАНСЛІТЕРАЦІЯ - генеруємо латиничні slug'и самі, щоб не залежати
# від сторонніх плагінів типу Cyr-To-Lat (які, як ми з'ясували,
# можуть мати власні баги і конфлікти з WooCommerce).
# ============================================================

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d",
    "е": "e", "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i",
    "ї": "yi", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ь": "", "ю": "iu", "я": "ia", "'": "", "’": "", "«": "", "»": "",
    # російські літери, яких немає в українському алфавіті - про всяк випадок
    "ё": "e", "ъ": "", "ы": "y", "э": "e",
}


def slugify_uk(text: str, max_length: int = 60) -> str:
    """Перетворює кириличний (або будь-який) текст у латиничний slug,
    придатний для URL: 'Зоотовари' -> 'zootovary'."""
    text = text.lower().strip()
    out = []
    for ch in text:
        if ch in _TRANSLIT_MAP:
            out.append(_TRANSLIT_MAP[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_length].rstrip("-") or "item"


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

# ---- ЗАХИСТ ВІД АВАРІЙНОГО СЦЕНАРІЮ ----
# Якщо фід партнера раптом стане недоступним/порожнім/урізаним (Prom впав,
# токен протух, партнер щось зламав) — без цього захисту скрипт вирішить,
# що "всі товари зникли", і масово поховає весь магазин. MIN_FEED_RATIO —
# мінімальна частка від попереднього успішного результату, нижче якої
# скрипт зупиняється і НІЧОГО не змінює в магазині.
MIN_FEED_RATIO = float(os.environ.get("MIN_FEED_RATIO", "0.7"))
STATE_FILE = SCRIPT_DIR / "sync_state.json"

# ---- САМОДІАГНОСТИКА КАТЕГОРІЙ ПАРТНЕРА ----
# Сюди щоразу зберігається свіжий список товарів, для яких chain_for_product()
# не знайшов ланцюжок у мапі партнера (тобто категорія була взята з
# product_type фіда - "чужа" Prom-таксономія). Читає і обробляє цей файл
# olibra_categories_scraper.py на своєму наступному запуску.
ORPHANS_FILE = SCRIPT_DIR / "pending_orphans.json"

# ---- ДЕДУПЛІКАЦІЯ МЕДІА ----
# url зображення -> id вкладення в медіатеці WordPress. Дивись великий
# коментар угорі файлу - без цього кешу WooCommerce плодить дублікати
# медіа при кожному оновленні товару/категорії.
MEDIA_CACHE_FILE = SCRIPT_DIR / "media_cache.json"

# ---- ЗАХИСТ ВІД ПАРАЛЕЛЬНОГО ЗАПУСКУ ----
# Потрібно, бо тепер синхронізацію можна запустити і за розкладом (cron/Task
# Scheduler), і вручну командою /sync в Telegram - без цього вони можуть
# накластися одна на одну. LOCK_STALE_SECONDS - якщо лок-файл старший за
# це значення, вважаємо, що попередній запуск "завис" і ігноруємо лок
# (щоб один завислий процес не заблокував синхронізацію назавжди).
LOCK_FILE = SCRIPT_DIR / "sync.lock"
LOCK_STALE_SECONDS = 2 * 60 * 60  # 2 години

# ---- TELEGRAM-СПОВІЩЕННЯ ПРО ПОМИЛКИ (опційно) ----
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---- TELEGRAM: "живий" прогрес одним повідомленням (опційно) ----
# Використовує ті самі TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID. Якщо вони
# не задані - notifier сам тихо вимикається, нічого не ламаючи.
notifier = TelegramNotifier(token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID)

# Розмір сторінки при запитах до WooCommerce (макс. 100)
WC_PER_PAGE = 100

# Пауза між запитами створення/оновлення товару (секунди). На слабкому сервері
# (старий CPU, мало RAM) без цього IIS одночасно піднімає забагато php-cgi
# процесів і навантаження на CPU впирається у 90-100%. Невелика затримка
# розтягує синхронізацію в часі, зате не кладе сервер.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.5"))

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


def notify_telegram(message: str):
    """Надсилає сповіщення в Telegram, якщо налаштовано. Тихо ігнорує помилки
    самого сповіщення (не можна, щоб впала сама сповіщувалка)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as exc:
        log.warning("Не вдалося надіслати сповіщення в Telegram: %s", exc)


def load_last_feed_count() -> int | None:
    import json
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_feed_count")
    except Exception:
        return None


def save_last_feed_count(count: int):
    import json
    STATE_FILE.write_text(json.dumps({"last_feed_count": count}), encoding="utf-8")


def load_media_cache() -> dict:
    if MEDIA_CACHE_FILE.exists():
        try:
            return json.loads(MEDIA_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Не вдалося прочитати %s - починаю з порожнього кешу медіа", MEDIA_CACHE_FILE)
            return {}
    return {}


def save_media_cache(cache: dict):
    MEDIA_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_image_refs(urls: list[str], media_cache: dict) -> tuple[list[dict], list[tuple[int, str]]]:
    """Перетворює список url зображень у payload для WooCommerce.
    Повертає (payload_images, misses), де misses - [(індекс, оригінальний_url), ...]
    для тих зображень, яких ще немає в media_cache - їх треба буде закешувати
    після отримання відповіді від WooCommerce (див. update_media_cache_from_response)."""
    payload_images = []
    misses = []
    for i, url in enumerate(urls):
        if not url:
            continue
        if url in media_cache:
            payload_images.append({"id": media_cache[url]})
        else:
            payload_images.append({"src": url})
            misses.append((i, url))
    return payload_images, misses


def update_media_cache_from_response(response_images: list[dict], misses: list[tuple[int, str]], media_cache: dict) -> bool:
    """Після успішного create/update товару звіряє відповідь WooCommerce з
    misses і дописує нові attachment_id в media_cache. Повертає True, якщо
    кеш змінився (треба зберегти на диск)."""
    changed = False
    for idx, original_url in misses:
        if idx < len(response_images):
            attach_id = response_images[idx].get("id")
            if attach_id:
                media_cache[original_url] = attach_id
                changed = True
    return changed


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
    link: str = ""  # потрібно для діагностики "сиріт" без повторного запиту до фіда
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
            link=g("link"),
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
        self.category_has_image: dict[int, bool] = {}  # заповнюється в get_all_categories()

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

    def create_product(self, payload: dict) -> dict:
        resp = _request_with_retry("POST", f"{self.base}/products", auth=self.auth, json=payload, timeout=60)
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce: %s", resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def update_product(self, product_id: int, payload: dict) -> dict:
        resp = _request_with_retry("PUT", f"{self.base}/products/{product_id}", auth=self.auth, json=payload, timeout=60)
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce: %s", resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def set_status(self, product_id: int, status: str):
        self.update_product(product_id, {"status": status})

    # ------------------------------------------------------------
    # Категорії
    # ------------------------------------------------------------

    def get_all_categories(self) -> dict:
        """Повертає {(parent_id, name_lower): id} для всіх категорій, що вже є.
        Заодно заповнює self.category_has_image, щоб CategoryTree знав, яким
        категоріям бракує фото (наприклад, після ручного видалення вкладень)."""
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
                self.category_has_image[c["id"]] = bool((c.get("image") or {}).get("src"))
            page += 1
        return result

    def create_category(self, name: str, parent_id: int, image_ref: dict | None = None) -> dict:
        payload = {"name": name, "parent": parent_id, "slug": slugify_uk(name)}
        if image_ref:
            payload["image"] = image_ref
        resp = _request_with_retry(
            "POST", f"{self.base}/products/categories",
            auth=self.auth,
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce (категорія '%s'): %s", name, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        self.category_has_image[data["id"]] = bool(image_ref)
        return data

    def update_category_image(self, category_id: int, image_ref: dict) -> dict:
        resp = _request_with_retry(
            "PUT", f"{self.base}/products/categories/{category_id}",
            auth=self.auth,
            json={"image": image_ref},
            timeout=60,
        )
        if not resp.ok:
            log.error("Тіло відповіді WooCommerce (фото категорії ID=%d): %s", category_id, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        self.category_has_image[category_id] = True
        return data


class CategoryTree:
    """
    Будує/кешує дерево категорій WooCommerce з рядка типу
    'Електроніка > Телефони > Смартфони'.

    Категорії, що вже існують у магазині, підхоплюються один раз на старті
    (щоб не створювати дублікати при повторних запусках), нові —
    створюються по ходу і додаються в той самий кеш.
    """

    def __init__(self, wc: "WooClient", media_cache: dict):
        self.wc = wc
        self.media_cache = media_cache
        self.cache = wc.get_all_categories()  # {(parent_id, name_lower): id}
        self.image_touched: set[int] = set()  # категорії, чиє фото вже перевірене/виставлене цього прогону

    def _image_ref(self, url: str) -> dict | None:
        if not url:
            return None
        if url in self.media_cache:
            return {"id": self.media_cache[url]}
        return {"src": url}

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
                data = self.wc.create_category(name, parent_id)
                leaf_id = data["id"]
                self.cache[key] = leaf_id
                log.info("Створено категорію '%s' (батько ID=%d)", name, parent_id)
            parent_id = leaf_id

        return leaf_id

    def resolve_from_chain(self, chain: list[tuple[str, str]]) -> Optional[int]:
        """
        Те саме, що resolve(), але приймає готовий ланцюжок [(назва, фото), ...]
        від кореня до листа - саме так, як його будує PartnerCategoryMap
        з даних, зібраних olibra_categories_scraper.py.

        Якщо категорія вже існує в магазині, але в неї немає фото (наприклад,
        після ручного видалення вкладень) - фото донастановлюється один раз
        за цей прогін (через self.image_touched, щоб не дьоргати WooCommerce
        API повторно для кожного товару цієї ж категорії).
        """
        if not chain:
            return None
        parent_id = 0
        leaf_id = None
        for name, image_url in chain:
            key = (parent_id, name.lower())
            if key in self.cache:
                leaf_id = self.cache[key]
                if image_url and leaf_id not in self.image_touched and not self.wc.category_has_image.get(leaf_id, False):
                    img_ref = self._image_ref(image_url)
                    is_new_upload = "src" in img_ref
                    data = self.wc.update_category_image(leaf_id, img_ref)
                    if is_new_upload:
                        attach_id = (data.get("image") or {}).get("id")
                        if attach_id:
                            self.media_cache[image_url] = attach_id
                    self.image_touched.add(leaf_id)
                    log.info("Донастановлено фото існуючій категорії '%s' (ID=%d)", name, leaf_id)
            else:
                img_ref = self._image_ref(image_url) if image_url else None
                is_new_upload = bool(img_ref) and "src" in img_ref
                data = self.wc.create_category(name, parent_id, image_ref=img_ref)
                leaf_id = data["id"]
                self.cache[key] = leaf_id
                if is_new_upload:
                    attach_id = (data.get("image") or {}).get("id")
                    if attach_id:
                        self.media_cache[image_url] = attach_id
                self.image_touched.add(leaf_id)
                log.info("Створено категорію '%s' (батько ID=%d)%s", name, parent_id,
                         " з фото" if img_ref else "")
            parent_id = leaf_id
        return leaf_id


class PartnerCategoryMap:
    """
    Читає файли, згенеровані скриптом-краулером сайту партнера
    (наприклад olibra_categories_scraper.py), і дає змогу знайти правильний
    ланцюжок категорій (з фото) для конкретного товару за його id з фіда.

    Якщо файли відсутні - просто вимкнено (products_by_id порожній),
    і скрипт синхронізації відкотиться на product_type з фіда як раніше.
    """

    def __init__(self, categories_file: Path, product_map_file: Path):
        self.enabled = categories_file.exists() and product_map_file.exists()
        self.nodes: dict[str, dict] = {}   # slug -> {name, parent_slug, image}
        self.product_to_slug: dict[str, str] = {}

        if not self.enabled:
            return

        for node in json.loads(categories_file.read_text(encoding="utf-8")):
            self.nodes[node["slug"]] = node
        self.product_to_slug = json.loads(product_map_file.read_text(encoding="utf-8"))
        log.info(
            "PartnerCategoryMap: завантажено %d категорій, %d товарів з мапи партнера",
            len(self.nodes), len(self.product_to_slug),
        )

    def chain_for_product(self, source_id: str) -> Optional[list[tuple[str, str]]]:
        if not self.enabled:
            return None
        slug = self.product_to_slug.get(source_id)
        if not slug or slug not in self.nodes:
            return None

        chain = []
        cur = slug
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = self.nodes.get(cur)
            if not node:
                break
            chain.append((node["name"], node.get("image") or ""))
            cur = node.get("parent_slug")
        chain.reverse()
        return chain or None


def build_payload(p: FeedProduct, media_cache: dict, category_id: Optional[int] = None) -> tuple[dict, list[tuple[int, str]]]:
    """Повертає (payload, image_misses). image_misses треба звірити з
    відповіддю WooCommerce після create/update, щоб дописати нові
    attachment_id в media_cache (див. update_media_cache_from_response)."""
    urls = ([p.image] if p.image else []) + list(p.extra_images)
    images, misses = resolve_image_refs(urls, media_cache)

    payload = {
        "name": p.title,
        "sku": p.sku,
        "slug": f"{slugify_uk(p.title, max_length=50)}-{p.source_id}",
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

    return payload, misses


# ============================================================
# КРОК 3. ГОЛОВНА ЛОГІКА СИНХРОНІЗАЦІЇ
# ============================================================

def sync():
    # ---- Крок 1: фід партнера. Якщо Prom/olibra недоступний — виходимо,
    # нічого в магазині не чіпаючи. ----
    try:
        feed_products = fetch_feed_products()
    except requests.RequestException as e:
        msg = f"⚠️ Prom-sync: не вдалося завантажити фід партнера — {e}"
        log.error(msg)
        notify_telegram(msg)
        sys.exit(1)

    # ---- Захист від "фід порожній/урізаний -> скрипт ховає весь магазин" ----
    last_count = load_last_feed_count()
    if last_count and len(feed_products) < last_count * MIN_FEED_RATIO:
        msg = (
            f"⚠️ Prom-sync: фід партнера повернув лише {len(feed_products)} товарів "
            f"(було {last_count}). Це менше порогу {int(MIN_FEED_RATIO * 100)}% — "
            f"схоже на збій, а не реальне зникнення товарів. Синхронізацію ЗУПИНЕНО, "
            f"магазин не змінено. Перевір фід вручну."
        )
        log.error(msg)
        notify_telegram(msg)
        sys.exit(1)

    # ---- Крок 2: WooCommerce. Якщо твій сервер (IIS) недоступний — теж просто виходимо. ----
    wc = WooClient(WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET)
    try:
        log.info("Отримую список товарів, які вже є у WooCommerce...")
        existing = wc.get_all_products_by_sku()
    except requests.RequestException as e:
        msg = f"⚠️ Prom-sync: WooCommerce/сайт недоступний — {e}"
        log.error(msg)
        notify_telegram(msg)
        sys.exit(1)

    log.info("У WooCommerce вже є %d товарів з префіксом '%s'", len(existing), SKU_PREFIX)

    media_cache = load_media_cache()
    log.info("Кеш медіа: %d вже завантажених зображень", len(media_cache))
    media_cache_dirty = False

    log.info("Завантажую/готую дерево категорій...")
    categories = CategoryTree(wc, media_cache)
    partner_map = PartnerCategoryMap(
        SCRIPT_DIR / "olibra_categories.json",
        SCRIPT_DIR / "olibra_product_map.json",
    )
    if partner_map.enabled:
        log.info("Використовую точну структуру категорій партнера (замість product_type з фіда)")
    else:
        log.info("Файли мапи партнера не знайдено - використовую product_type з фіда")

    created, updated, failed = 0, 0, 0
    seen_skus = set()
    missing_skus = set()
    orphans: list[FeedProduct] = []  # товари без ланцюжка з мапи партнера (fallback на product_type)

    notifier.start(total=len(feed_products), label="Prom-sync: Olibra → WooCommerce")

    for idx, p in enumerate(feed_products, 1):
        seen_skus.add(p.sku)
        category_id = None
        try:
            chain = partner_map.chain_for_product(p.source_id)
            if chain:
                category_id = categories.resolve_from_chain(chain)
            elif p.category_path:
                category_id = categories.resolve(p.category_path)
                if partner_map.enabled:
                    # Мапа партнера увімкнена, але саме для цього товару
                    # ланцюжка не знайшлось - фіксуємо як "сироту" для подальшого
                    # автоматичного аналізу скрапером (olibra_categories_scraper.py)
                    orphans.append(p)
        except requests.HTTPError as e:
            log.error("Не вдалося створити/знайти категорію для %s: %s", p.sku, e)

        payload, img_misses = build_payload(p, media_cache, category_id)
        try:
            if p.sku in existing:
                # ВАЖЛИВО: не шлемо 'sku' при оновленні. Це офіційно підтверджений
                # баг WooCommerce (github.com/woocommerce/woocommerce/issues/33806) -
                # PUT з тим самим SKU, який товар вже має, іноді помилково
                # трактується як "SKU вже зайнятий іншим товаром". SKU й так
                # не змінюється при оновленні, тому просто прибираємо поле.
                update_payload = {k: v for k, v in payload.items() if k not in ("sku", "slug")}
                result = wc.update_product(existing[p.sku], update_payload)
                updated += 1
            else:
                result = wc.create_product(payload)
                created += 1

            if img_misses:
                if update_media_cache_from_response(result.get("images", []), img_misses, media_cache):
                    media_cache_dirty = True
        except requests.HTTPError as e:
            failed += 1
            log.error("Помилка для товару %s (%s): %s", p.sku, p.title, e)
            notifier.error(f"Товар {p.sku} ({p.title}): {e}")

        notifier.progress(processed=idx, added=created, updated=updated, errors=failed)

        # Періодично зберігаємо кеш медіа, щоб не втратити накопичене при
        # аварійному перериванні посеред довгого прогону.
        if media_cache_dirty and idx % 50 == 0:
            save_media_cache(media_cache)
            media_cache_dirty = False

        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    if DEACTIVATE_MISSING:
        missing_skus = set(existing.keys()) - seen_skus
        for sku in missing_skus:
            try:
                wc.set_status(existing[sku], "draft")
                log.info("Товар %s відсутній у фіді — переведено в чернетки", sku)
            except requests.HTTPError as e:
                log.error("Не вдалося приховати %s: %s", sku, e)

    if media_cache_dirty:
        save_media_cache(media_cache)
    log.info("Кеш медіа: %d записів після синхронізації", len(media_cache))

    # Зберігаємо свіжий список "сиріт" - його прочитає і опрацює
    # olibra_categories_scraper.py на своєму наступному запуску.
    orphans_out = [
        {"id": o.source_id, "title": o.title, "link": o.link, "category_path": o.category_path}
        for o in orphans
    ]
    ORPHANS_FILE.write_text(json.dumps(orphans_out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(
        "%d товарів без категорії партнера (fallback на product_type) - збережено в %s",
        len(orphans_out), ORPHANS_FILE,
    )

    save_last_feed_count(len(feed_products))

    summary = (
        f"Готово. Створено: {created}, оновлено: {updated}, помилок: {failed}, "
        f"приховано: {len(missing_skus) if DEACTIVATE_MISSING else 0}"
    )
    log.info(summary)

    notifier.finish(
        added=created,
        updated=updated,
        errors=failed,
        extra_note=f"Приховано: {len(missing_skus) if DEACTIVATE_MISSING else 0}",
    )


if __name__ == "__main__":
    if LOCK_FILE.exists() and (time.time() - LOCK_FILE.stat().st_mtime) < LOCK_STALE_SECONDS:
        log.warning(
            "Синхронізація вже виконується (знайдено %s) - цей запуск пропущено, "
            "щоб не накластися на попередній.", LOCK_FILE,
        )
        notify_telegram("⏳ Prom-sync: попередній запуск ще виконується — цей запуск пропущено.")
        sys.exit(0)

    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    try:
        sync()
    except Exception as e:
        log.exception("Синхронізація завершилась з критичною помилкою")
        notify_telegram(f"🔴 Prom-sync: критична помилка — {e}")
        sys.exit(1)
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass