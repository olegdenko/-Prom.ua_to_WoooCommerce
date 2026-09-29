"""
description_generator.py

Модуль генерації ПОВНОГО HTML-опису товару (поле `description` у WooCommerce)
через OpenAI — окремо від seo_generator.py, який заповнює лише мета-поля
Rank Math (заголовок/опис/фокус-слово для пошукової видачі).

Навіщо окремо: `prom_woo_sync.py` записує description/short_description з
фіда партнера ЛИШЕ ОДИН РАЗ, при створенні товару (див. build_payload(),
is_update=True не включає ці поля) — тобто наступні щоденні синхронізації
його більше не чіпають. Це і дозволяє безпечно переписати опис один раз
через цей модуль і не боятись, що завтрашній sync() його затре.

Використання:
  - автономно (вся база):     python3 description_generator.py --all
  - точково (один товар):     python3 description_generator.py --product-id 12345
  - перевірка налаштувань:    python3 description_generator.py --check
  - як імпорт у sync-скрипт:  from description_generator import process_single_product

ПРИНЦИПИ БЕЗПЕКИ — ідентичні seo_generator.py:
  - Секрети (WC_URL/ключі, OPENAI_API_KEY) — з того самого prom_woo_sync.env.
  - За замовчуванням лише товари з SKU, що починається на SKU_PREFIX.
  - Ніколи не кидає виняток назовні з process_single_product()/process_all_*().
  - При вичерпанні коштів/ліміту OpenAI чи невалідному ключі — одразу вимикається
    до кінця поточного запуску (не б'ється в закриті двері 1400 разів).
  - За замовчуванням товар з уже "повним" описом (не менше DESCRIPTION_MIN_WORDS
    символів) ПРОПУСКАЄТЬСЯ — байдуже, наш це був опис чи вручну написаний.
    Перезапис — тільки свідомо, через overwrite=True / --overwrite.

УВАГА: якщо цей модуль імпортується РАЗОМ із seo_generator.py в одному файлі
(наприклад, у prom_woo_sync.py), обидва мають функцію process_single_product() —
імпортуйте з псевдонімом, наприклад:
    from seo_generator import process_single_product as fill_seo
    from description_generator import process_single_product as fill_description
"""

from __future__ import annotations

import os
import re
import sys
import time
import argparse
import logging
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Той самий спільний .env, що й у prom_woo_sync.py / seo_generator.py
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
load_dotenv(ENV_PATH)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [description_generator] %(levelname)s: %(message)s",
)
log = logging.getLogger("description_generator")

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------
WC_URL = os.environ.get("WC_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.environ.get("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.environ.get("WC_CONSUMER_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SKU_PREFIX = os.environ.get("SKU_PREFIX", "OLB-")
DESC_OPENAI_MODEL = os.environ.get("DESC_OPENAI_MODEL", "gpt-4o-mini")
DESC_REQUEST_DELAY = float(os.environ.get("DESC_REQUEST_DELAY", "1.0"))
# Скільки символів "живого" тексту (без HTML-тегів) вважати "вже повним
# описом", який більше не чіпаємо без --overwrite.
DESCRIPTION_MIN_WORDS = int(os.environ.get("DESCRIPTION_MIN_WORDS", "500"))
MAX_RETRIES = 3

SEO_ENABLED = True
_DISABLE_REASON: str | None = None


def _startup_disable(reason: str):
    global SEO_ENABLED, _DISABLE_REASON
    SEO_ENABLED = False
    _DISABLE_REASON = reason
    log.warning(f"⚠️ Модуль опису вимкнено: {reason}")


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
RateLimitError = AuthenticationError = PermissionDeniedError = APIError = Exception

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

_RUNTIME_DISABLED_REASON: str | None = None


def _seo_available() -> bool:
    return SEO_ENABLED and _RUNTIME_DISABLED_REASON is None


def _disable_at_runtime(reason: str):
    global _RUNTIME_DISABLED_REASON
    if _RUNTIME_DISABLED_REASON is None:
        _RUNTIME_DISABLED_REASON = reason
        log.error(f"🔴 Генерація описів вимкнена до кінця цього запуску: {reason}")


def is_description_disabled() -> str | None:
    """Причина, чому генерація описів недоступна прямо зараз, або None."""
    if not SEO_ENABLED:
        return _DISABLE_REASON
    return _RUNTIME_DISABLED_REASON


def _is_quota_exhausted(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient_quota" in text or "exceeded your current quota" in text


_TAG_RE = re.compile(r"<[^>]+>")


def _text_length(html: str) -> int:
    """Довжина 'живого' тексту без HTML-розмітки — щоб не плутати короткий
    опис, обгорнутий у <p>, з дійсно розгорнутою статтею."""
    return len(_TAG_RE.sub("", html or "").strip())


def _word_count(html: str) -> int:
    """Кількість слів живого тексту (без HTML-тегів) — та сама метрика, за
    якою рахує Rank Math, на відміну від довжини в символах."""
    return len(_TAG_RE.sub(" ", html or "").split())


def _has_full_description(product: dict) -> bool:
    return _word_count(product.get("description", "")) >= DESCRIPTION_MIN_WORDS


def _owns_product(product: dict, enforce_sku: bool) -> bool:
    if not enforce_sku:
        return True
    sku = product.get("sku") or ""
    return sku.startswith(SKU_PREFIX)


def _category_links(product: dict) -> str:
    """Реальні посилання на категорії ЦЬОГО товару — щоб GPT не вигадував URL."""
    links = []
    for c in product.get("categories", []):
        slug = c.get("slug")
        name = c.get("name")
        if slug and name:
            links.append(f'- {name}: {WC_URL}/product-category/{slug}/')
    return "\n".join(links) if links else "(немає категорій — внутрішніх посилань не вставляй)"


# Невеликий, СВІДОМО обмежений і вручну перевірений список зовнішніх
# посилань на статті Вікіпедії про матеріали. GPT сам URL НЕ вигадує —
# тільки обирає (максимум одне) зі списку нижче за збігом ключових слів
# у назві/категоріях товару. Це навмисно, щоб ніколи не отримати бите
# посилання: розширюйте список самі під свій асортимент за тим самим
# принципом — перевірений вручну URL, а не згенерований моделлю.
EXTERNAL_LINK_WHITELIST: list[tuple[list[str], str, str]] = [
    (["дерев'ян", "дерево", "деревин"], "Деревина", "https://uk.wikipedia.org/wiki/Деревина"),
    (["поліпропілен", "пластик"], "Поліпропілен", "https://uk.wikipedia.org/wiki/Поліпропілен"),
    (["паперов", "папір"], "Папір", "https://uk.wikipedia.org/wiki/Папір"),
    (["картон"], "Картон", "https://uk.wikipedia.org/wiki/Картон"),
    (["целюлоз", "бамбук", "тростин"], "Целюлоза", "https://uk.wikipedia.org/wiki/Целюлоза"),
]


def _external_link(product: dict) -> str:
    """Підбирає ОДНЕ перевірене зовнішнє посилання за ключовими словами в
    назві/категоріях товару, або повертає порожньо, якщо збігу немає."""
    haystack = (product.get("name", "") + " " + " ".join(
        c.get("name", "") for c in product.get("categories", [])
    )).lower()
    for keywords, label, url in EXTERNAL_LINK_WHITELIST:
        if any(kw in haystack for kw in keywords):
            return f"{label}: {url}"
    return "(немає релевантного — зовнішнього посилання не вставляй)"


def _focus_keyword(product: dict) -> str:
    """Фокусне ключове слово, яке вже згенерував seo_generator.py (якщо
    його ще немає — модуль опису однаково працює, просто без жорсткої
    прив'язки до фрази; тому рекомендовано спершу запускати fill_seo())."""
    for m in product.get("meta_data", []):
        if m.get("key") == "rank_math_focus_keyword" and m.get("value"):
            return m["value"]
    return ""


def generate_description_html(product: dict) -> str | None:
    """Генерує повний HTML-опис товару. Ніколи не кидає виняток — None при
    будь-якій проблемі."""
    if not _seo_available():
        return None

    title = product.get("name", "")
    categories = ", ".join(c["name"] for c in product.get("categories", []))
    source_description = _TAG_RE.sub("", product.get("description", "") or "")[:600]
    links = _category_links(product)
    external_link = _external_link(product)
    focus_keyword = _focus_keyword(product)

    keyword_block = (
        f'Фокусне ключове слово для цього товару: "{focus_keyword}". ОБОВ\'ЯЗКОВО:\n'
        f'- вжити цю точну фразу в першому реченні тексту;\n'
        f'- вжити її дослівно щонайменше в одному з заголовків <h2>;\n'
        f'- природно повторити її ще 2-3 рази по тексту (не більше, без переспаму).'
        if focus_keyword else
        "Фокусного ключового слова для товару ще не згенеровано (запустіть спочатку "
        "seo_generator.py) — пиши без жорсткої прив'язки до конкретної фрази."
    )

    prompt = f"""
Ти контент-маркетолог інтернет-магазину DENKO (одноразовий посуд та HoReCa-товари).
Напиши розгорнутий SEO-опис товару українською мовою у форматі готового HTML
для поля опису товару WooCommerce.

Дані товару:
- Назва: {title}
- Категорії: {categories or "не вказано"}
- Короткий опис від постачальника (лише контекст, не копіюй дослівно): {source_description or "не надано"}

{keyword_block}

Дозволені внутрішні посилання на категорії (використовуй ТІЛЬКИ ці, природно
вплітаючи в текст 1-2 з них; нових URL не вигадуй):
{links}

Дозволене ЗОВНІШНЄ посилання (використай РІВНО ОДИН РАЗ, якщо воно надане
нижче і дійсно релевантне — атрибути target="_blank" rel="noopener", БЕЗ
rel="nofollow"; якщо написано "немає релевантного" — зовнішніх посилань не додавай):
{external_link}

Обов'язкова структура:
1. Вступний абзац (2-3 речення), назва товару виділена <strong>.
2. Блок змісту: <div class="toc-block"><p>Зміст статті:</p><ul><li><a href="#sectionN">...</a></li>...</ul></div>
3. 4-5 розділів <h2 id="sectionN">Заголовок</h2> з текстом та за потреби <ul>/<ol>:
   розкрий переваги й характеристики, сфери застосування, догляд/безпеку чи
   екологічність (адаптуй саме під цей тип товару, а не копіюй сліпо ці назви),
   і окремо — чому купувати саме в DENKO.
4. Завершальний абзац із закликом до дії.

Жорсткі вимоги:
- МІНІМУМ 600 слів живого тексту (Rank Math вимагає не менше 600 — це важливо,
  не скорочуй розділи).
- НЕ вигадуй конкретні характеристики (розміри, матеріал, сертифікати), яких
  не було в наданих даних — якщо їх не передано, пиши узагальнено, без цифр.
- Тон практичний, для B2B/HoReCa аудиторії, без перебільшень.
- У відповіді — ЛИШЕ готовий HTML-фрагмент, без markdown-огорток (```), без
  пояснень до чи після нього.
"""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=DESC_OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
            )
            html = response.choices[0].message.content.strip()
            # На випадок якщо модель все ж обгорне відповідь у ```html ... ```
            html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html.strip())

            word_count = _word_count(html)
            has_kw = (not focus_keyword) or (focus_keyword.lower() in _TAG_RE.sub(" ", html).lower())
            if word_count < 100:
                log.error("Відповідь OpenAI підозріло коротка, пропускаю")
                return None
            if (word_count < 550 or not has_kw) and attempt < MAX_RETRIES:
                log.warning(
                    f"Спроба {attempt}: опис не пройшов перевірку "
                    f"(слів: {word_count}, ключове слово присутнє: {has_kw}), повторюю"
                )
                continue
            if word_count < 550 or not has_kw:
                log.warning(
                    f"Опис збережено із зауваженнями (слів: {word_count}, "
                    f"ключове слово присутнє: {has_kw}) — Rank Math може досі скаржитись"
                )
            return html

        except RateLimitError as e:
            if _is_quota_exhausted(e):
                _disable_at_runtime(f"вичерпано ліміт/кошти OpenAI ({e})")
                return None
            wait = 2 ** attempt
            log.warning(f"OpenAI 429, спроба {attempt}/{MAX_RETRIES}, чекаю {wait}с")
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

        except Exception as e:
            log.exception(f"Неочікувана помилка при генерації опису: {e}")
            return None

    log.error("Вичерпано спроби звернення до OpenAI (rate limit)")
    return None


def _build_image_alt_payload(product: dict, focus_keyword: str) -> list[dict] | None:
    """Той самий принцип, що й у seo_generator.py: проставляє alt-текст
    (фокусне слово + назва товару) наявним зображенням товару, зберігаючи
    їхні id/порядок. Дублюється навмисно — щоб alt виставлявся навіть якщо
    цей модуль запускають окремо, без seo_generator.py."""
    images = product.get("images", [])
    if not images or not focus_keyword:
        return None
    alt_text = f"{focus_keyword} — {product.get('name', '')}"[:125]
    return [{"id": img["id"], "alt": alt_text} for img in images if img.get("id")]


def update_product_description(product: dict) -> bool:
    """Записує згенерований опис у товар. Ніколи не кидає виняток назовні."""
    try:
        p_id = product["id"]
        log.info(f"Генерація опису для товару ID {p_id}: {product.get('name')}")

        html = generate_description_html(product)
        if not html:
            return False

        payload = {"description": html}
        img_payload = _build_image_alt_payload(product, _focus_keyword(product))
        if img_payload:
            payload["images"] = img_payload

        res = wcapi.put(f"products/{p_id}", payload)
        if res.status_code == 200:
            log.info(f"✅ Опис успішно оновлено для ID {p_id} ({_text_length(html)} символів тексту)")
            return True

        log.error(f"❌ Помилка оновлення WooCommerce для ID {p_id}: {res.text}")
        return False

    except requests.RequestException as e:
        log.error(f"Мережева помилка при оновленні опису товару: {e}")
        return False
    except Exception as e:
        log.exception(f"Неочікувана помилка при оновленні опису товару: {e}")
        return False


def process_single_product(product_id: int, enforce_sku: bool = True, overwrite: bool = False) -> bool:
    """
    Точка входу для інтеграції з prom_woo_sync.py. Безпечна для виклику з
    основного циклу синхронізації — ніколи не кидає виняток.

    overwrite=False (за замовчуванням) — товар з уже "повним" описом (довшим
        за DESCRIPTION_MIN_LENGTH символів живого тексту), байдуже наш він чи
        вручну написаний, — пропускається.
    overwrite=True — опис перегенеровується й перезаписується примусово.
    """
    if not _seo_available():
        log.info(f"Пропуск ID {product_id} — модуль опису вимкнено ({is_description_disabled()})")
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

    if _has_full_description(product) and not overwrite:
        log.info(f"Пропуск ID {product_id} — опис уже повний (≥{DESCRIPTION_MIN_WORDS} слів).")
        return False

    return update_product_description(product)


def process_all_unfilled_products(enforce_sku: bool = True, per_page: int = 50, overwrite: bool = False):
    """Масова обробка товарів з коротким описом (перший повний прогін / --all)."""
    if not _seo_available():
        log.error(f"Модуль опису вимкнено, масову обробку не запущено: {is_description_disabled()}")
        return

    page = 1
    processed = skipped = failed = 0

    while True:
        if not _seo_available():
            log.error(f"Зупиняюсь: {is_description_disabled()}")
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
            break

        for product in products:
            if not _seo_available():
                log.error(f"Зупиняюсь посеред сторінки {page}: {is_description_disabled()}")
                break

            if not _owns_product(product, enforce_sku):
                skipped += 1
                continue
            if _has_full_description(product) and not overwrite:
                log.info(f"Пропуск ID {product['id']} — опис уже повний.")
                skipped += 1
                continue

            ok = update_product_description(product)
            processed += int(ok)
            failed += int(not ok)
            time.sleep(DESC_REQUEST_DELAY)

        page += 1

    log.info(f"Готово. Оброблено: {processed}, пропущено: {skipped}, помилок: {failed}")
    if is_description_disabled():
        log.error(f"Прогін завершено достроково: {is_description_disabled()}")


def _cli():
    parser = argparse.ArgumentParser(description="Генератор повних описів товарів через OpenAI")
    parser.add_argument("--all", action="store_true", help="Обробити всю базу товарів з коротким описом")
    parser.add_argument("--product-id", type=int, help="Обробити один конкретний товар за ID")
    parser.add_argument(
        "--all-sku", action="store_true",
        help="Не обмежуватись SKU_PREFIX — обробляти будь-які товари (обережно!)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Перезаписати опис навіть там, де він уже повний (в т.ч. написаний вручну)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Тільки перевірити налаштування (ключі, пакети) і вийти",
    )
    args = parser.parse_args()

    if args.check:
        if SEO_ENABLED:
            print(f"✅ Модуль опису готовий до роботи. Модель: {DESC_OPENAI_MODEL}, WC_URL: {WC_URL}")
            sys.exit(0)
        else:
            print(f"❌ Модуль опису НЕ готовий: {_DISABLE_REASON}")
            sys.exit(1)

    if not SEO_ENABLED:
        print(f"❌ Модуль опису вимкнено: {_DISABLE_REASON}\nЗапустіть 'python3 description_generator.py --check' для деталей.")
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
