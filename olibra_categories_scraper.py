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
Зіставляє товари з правильними категоріями (групами) сайту партнера
olibra.com.ua, а не із загальною таксономією Prom.ua (яку віддає фід).

Чому це потрібно:
Google Merchant фід партнера (product_type) відображає ЗАГАЛЬНУ таксономію
Prom.ua, а не власну структуру груп на сайті партнера. Цей скрипт натомість
проходить по реальних сторінках груп сайту (olibra.com.ua) і будує точну
відповідність: товар -> група, а також забирає фото кожної групи.

РЕКУРСИВНЕ ВИЯВЛЕННЯ ПІДКАТЕГОРІЙ (важлива відмінність від старої версії):
Раніше TREE був повністю статичним, ручним списком, зібраним з сайдбар-меню
сайту (те саме меню повторюється на КОЖНІЙ сторінці). Але на сторінці кожної
групи, окрім сайдбару, може бути ЩЕ ОДНА, глибша секція - "плитки" підкатегорій,
специфічні саме для цієї групи (наприклад, "Пакети з пластиковою ручкою"
на сайті має підгрупи "35*35 Дніпро", "40*40 Дніпро", "40*45 Дніпро" - вони НЕ
показані в сайдбарі, тільки на сторінці самої групи). TREE нижче більше не
претендує на повноту - це лише СТАРТОВИЙ НАБІР (той самий сайдбар), а решту
дерева скрипт тепер добудовує сам:
  1. known_slugs = всі slug'и з TREE (це і є "шум" сайдбару - він однаковий
     на кожній сторінці сайту).
  2. Для кожної групи з TREE (і кожної щойно знайденої підгрупи) качаємо її
     сторінку, шукаємо ВСІ посилання виду /ua/g<число>-<slug>, віднімаємо
     known_slugs - залишок це і є справжні, раніше невідомі підкатегорії
     САМЕ цієї групи (сайдбарний шум відкидається автоматично, бо він завжди
     збігається з known_slugs).
  3. Кожну нову підкатегорію додаємо в known_slugs і в чергу на обхід - так
     рекурсивно знаходяться підкатегорії будь-якої глибини, поки на черговій
     сторінці більше не з'являється нічого нового.
Результат зберігається у progress-файлі (ключ "discovered"), тому наступні
запуски не проходять цей етап заново для вже перевірених груп.

Результат зберігається у два файли:
  - olibra_categories.json     - дерево категорій (назва, slug, батько, фото)
  - olibra_product_map.json    - {product_id: category_slug} для кожного товару

Потім prom_woo_sync.py читає ці файли, щоб призначати товарам правильні
категорії (замість product_type з фіда) і виставляти фото категоріям.

Запуск:
    python3 olibra_categories_scraper.py

Це довгий процес (сотні запитів до сайту партнера з паузами, щоб не
перевантажувати їхній сервер) - очікуй 10-20+ хвилин (тепер трохи довше через
рекурсивне виявлення підкатегорій). Прогрес зберігається по ходу, тому скрипт
можна перервати і запустити знову - вже оброблені групи не будуть оброблятись
повторно.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = "https://olibra.com.ua"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CategoryMapBot/1.0)"}
DELAY = 1.0  # пауза між запитами (секунди) - ввічливо до сервера партнера
MAX_PAGES_PER_GROUP = 60  # запобіжник від нескінченного циклу пагінації
MAX_DISCOVERED_NODES = 3000  # запобіжник від "втечі" рекурсії, якщо десь на
                              # сторінці випадково знайдеться посилання, що не
                              # є справжньою підкатегорією (напр. "схожі товари")

OUT_DIR = Path(__file__).resolve().parent
CATEGORIES_FILE = OUT_DIR / "olibra_categories.json"
PRODUCT_MAP_FILE = OUT_DIR / "olibra_product_map.json"
PROGRESS_FILE = OUT_DIR / "olibra_scrape_progress.json"

# ---- Самодіагностика "сиріт" з prom_woo_sync.py (self-healing) ----
# Роль цього файлу зменшилась відколи додалось рекурсивне виявлення підкатегорій
# вище - більшість "нових глибших підгруп" тепер знаходяться самі. Але
# process_pending_orphans() лишається корисною для окремого випадку: коли
# товар зник з категорії через застарілий прогрес (done_groups), а не через
# відсутність самої категорії в дереві.
ORPHANS_FILE = OUT_DIR / "pending_orphans.json"
DELAY_ORPHAN_CHECK = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("olibra_scraper")

# ============================================================
# СТАРТОВИЙ набір категорій сайту партнера (зібраний з сайдбар-меню, яке
# повторюється на кожній сторінці сайту). Формат: (Назва, slug групи,
# slug батьківської групи або None).
#
# ВАЖЛИВО: це більше НЕ повний список - лише стартові точки для рекурсивного
# обходу (див. discover_children() нижче). Онови цей список вручну, тільки
# якщо партнер додасть зовсім НОВИЙ верхньорівневий розділ у сайдбарі -
# сайдбар-меню міняється рідко, тому періодичний ручний перегляд є ок. Все,
# що глибше (3-й, 4-й рівень) - скрипт знаходить сам.
# ============================================================
TREE = [
    ("Пакети Екологія", "g99658714-paketi-ekologiya", None),
    ("Пакети і упаковка", "g4551008-paketi-upakovka", None),
    ("Пакет «майка»", "g4550966-paket-majka", "g4551008-paketi-upakovka"),
    ("Пакет «фасовка»", "g4550984-paket-fasovka", "g4551008-paketi-upakovka"),
    ("Пакет «петля»", "g4552269-paket-petlya", "g4551008-paketi-upakovka"),
    ("Пакет «банан»", "g5021424-paket-banan", "g4551008-paketi-upakovka"),
    ("Пакети з пластиковою ручкою", "g6501999-paketi-plastikovoyu-ruchkoyu", "g4551008-paketi-upakovka"),
    ("Паперові подарункові пакети", "g52802083-paperovi-podarunkovi-paketi", "g4551008-paketi-upakovka"),
    ("Пакети паперові", "g10189899-paketi-paperovi", "g4551008-paketi-upakovka"),
    ("Сумка господарська", "g6781518-sumka-gospodarska", "g4551008-paketi-upakovka"),
    ("Скотч", "g4690327-skotch", "g4551008-paketi-upakovka"),
    ("Стретч-плівка", "g4808112-stretch-plivka", "g4551008-paketi-upakovka"),
    ("Пакети zip-lock", "g5389362-paketi-zip-lock", "g4551008-paketi-upakovka"),
    ("Одноразовий посуд", "g4551230-odnorazovij-posud", None),
    ("Паперові стаканчики", "g4842715-paperovi-stakanchiki", "g4551230-odnorazovij-posud"),
    ("Столові прибори", "g19072721-stolovi-pribori", "g4551230-odnorazovij-posud"),
    ("Паперові тарілки", "g19073059-paperovi-tarilki", "g4551230-odnorazovij-posud"),
    ("Зубочистки", "g19075818-zubochistki", "g4551230-odnorazovij-posud"),
    ("Пластикові стаканчики", "g19076054-plastikovye-stakanchiki", "g4551230-odnorazovij-posud"),
    ("Пластикові тарілки", "g19080425-plastikovye-tarelki", "g4551230-odnorazovij-posud"),
    ("Прикраси для напоїв та їжі", "g19120215-prikrasi-dlya-napoyiv", "g4551230-odnorazovij-posud"),
    ("Упаковка для ланчу", "g87860634-upakovka-dlya-lancha", "g4551230-odnorazovij-posud"),
    ("Паковання з впіненого полістиролу", "g88335032-upakovka-vpenennogo-polistirola", "g4551230-odnorazovij-posud"),
    ("Купольні стакани", "g154236003-kupolni-stakani", "g4551230-odnorazovij-posud"),
    ("Бокси для ягід", "g154265341-boksi-dlya-yagid", "g4551230-odnorazovij-posud"),
    ("Пластикова продукція", "g53151841-plastikova-produktsiya", None),
    ("Продуктові контейнери", "g20635732-produktovi-kontejneri", "g53151841-plastikova-produktsiya"),
    ("Відро пластикове", "g6182170-vidro-plastikove", "g53151841-plastikova-produktsiya"),
    ("Батарейки", "g4545501-batarejki", None),
    ("Duracell", "g4801822-duracell", "g4545501-batarejki"),
    ("GP", "g4839512-gp", "g4545501-batarejki"),
    ("Kodak", "g4839539-kodak", "g4545501-batarejki"),
    ("Таблетка", "g17212525-tabletka", "g4545501-batarejki"),
    ("Toshiba", "g68299619-toshiba", "g4545501-batarejki"),
    ("Panasonic", "g4839551-panasonic", "g4545501-batarejki"),
    ("Паперова продукція (серветки, туалетний папір, рушники)", "g4551006-paperova-produktsiya-servetki", None),
    ("Папір туалетний", "g7139599-papir-tualetnij", "g4551006-paperova-produktsiya-servetki"),
    ("Рушники", "g83612675-rushniki", "g4551006-paperova-produktsiya-servetki"),
    ("Серветки", "g83636291-servetki", "g4551006-paperova-produktsiya-servetki"),
    ("Побутова хімія", "g29764010-pobutova-himiya", None),
    ("Засоби для прання білизни", "g18366051-zasobi-dlya-prannya", "g29764010-pobutova-himiya"),
    ("Засоби для прибирання", "g29764606-zasobi-dlya-pribirannya", "g29764010-pobutova-himiya"),
    ("Засоби для миття посуду", "g13847868-zasobi-dlya-mittya", "g29764010-pobutova-himiya"),
    ("Родентицидні і інсектицидні засоби", "g11847817-rodentitsidni-insektitsidni-zasobi", "g29764010-pobutova-himiya"),
    ("Освіжувачі повітря", "g75125295-osvizhuvachi-povitrya", "g29764010-pobutova-himiya"),
    ("Догляд, гігієна, косметика", "g30335791-doglyad-gigiyena-kosmetika", None),
    ("Догляд за волоссям", "g30337394-doglyad-volossyam", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Ватні диски та палички", "g6163789-vatnye-diski-palochki", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Приладдя для гоління", "g16809924-prinadlezhnosti-dlya-britya", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Презервативи", "g11544582-prezervativi", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Вологі серветки", "g6107752-vlazhnye-salfetki", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Косметичні серветки", "g29765208-kosmeticheskie-salfetki", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Жіночі гігієнічні прокладки", "g30277414-zhenskie-gigienicheskie-prokladki", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Мило", "g13847906-milo", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Догляд за порожниною рота", "g51914285-uhod-polostyu-rta", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Вата медична", "g60996312-vata-medichna", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Марля медична", "g60996336-marlya-meditsinskaya", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Гелі для душу", "g83772623-geli-dlya-dusha", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Дезодоранти", "g90025853-dezodoranty", "g30335791-doglyad-gigiyena-kosmetika"),
    ("Господарські товари", "g4551235-gospodarski-tovari", None),
    ("Пакети для сміття", "g4707120-paketi-dlya-smittya", "g4551235-gospodarski-tovari"),
    ("Рукавички", "g4746849-rukavichki", "g4551235-gospodarski-tovari"),
    ("Ганчірки, губки, серветки, шкребки для прибирання", "g4780476-ganchirki-gubki-servetki", "g4551235-gospodarski-tovari"),
    ("Губки лазневі", "g6419113-gubki-laznevi", "g4551235-gospodarski-tovari"),
    ("Рукав, фольга, пергамент", "g4793501-rukav-folga-pergament", "g4551235-gospodarski-tovari"),
    ("Клеї для побутових потреб", "g10207758-kleyi-dlya-pobutovih", "g4551235-gospodarski-tovari"),
    ("Прищіпки", "g8364120-prischepki", "g4551235-gospodarski-tovari"),
    ("Для одягу та взуття", "g4776889-dlya-odyagu-vzuttya", "g4551235-gospodarski-tovari"),
    ("Запальнички, газ для запальничок", "g4779463-zapalnichki-gaz-dlya", None),
    ("Cricket", "g17045366-cricket", "g4779463-zapalnichki-gaz-dlya"),
    ("X Fox", "g17049326-fox", "g4779463-zapalnichki-gaz-dlya"),
    ("Bic", "g17049616-bic", "g4779463-zapalnichki-gaz-dlya"),
    ("Газ для запальничок", "g6651488-gaz-dlya-zazhigalok", "g4779463-zapalnichki-gaz-dlya"),
    ("Канцтовари", "g6731272-kantstovari", None),
    ("Tukzar", "g18476928-tukzar", "g6731272-kantstovari"),
    ("Цінники", "g18852796-tsinniki", "g6731272-kantstovari"),
    ("Цукор в стіках", "g60203705-tsukor-kava", None),
    ("Свічки", "g4927899-svichki", None),
    ("Лампочки", "g22011410-lampochki", None),
    ("Biom", "g68254607-biom", "g22011410-lampochki"),
]

# ФІКС: сайт олібри використовує ВІДНОСНІ href (/ua/p123-slug.html), а не
# абсолютні (https://olibra.com.ua/ua/p123-slug.html). Стара регулярка
# вимагала повний домен у href і тому ніколи нічого не знаходила -
# домен тепер опціональний.
PRODUCT_RE = re.compile(r'href="(?:https://olibra\.com\.ua)?/ua/p(\d+)-[^"]*\.html"')
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')

# НОВЕ: посилання на ГРУПИ (не товари) - формат /ua/g<число>-<slug>, без .html.
# Використовується для рекурсивного виявлення підкатегорій.
GROUP_LINK_RE = re.compile(r'href="(?:https://olibra\.com\.ua)?/ua/(g\d+-[^"?#]+)"')
# Якщо посилання на групу має атрибут title="..." (так зроблено у плитках
# підкатегорій на сторінці групи) - беремо звідти чисту назву, без потреби
# робити для цього окремий запит.
GROUP_LINK_WITH_TITLE_RE = re.compile(
    r'href="(?:https://olibra\.com\.ua)?/ua/(g\d+-[^"?#]+)"[^>]*title="([^"]+)"'
)
H1_RE = re.compile(r"<h1[^>]*>([^<]+)</h1>")


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            log.warning("HTTP %s для %s", resp.status_code, url)
            return None
        return resp.text
    except requests.RequestException as e:
        log.warning("Помилка запиту %s: %s", url, e)
        return None


def get_group_image(slug: str) -> str | None:
    html = fetch(f"{BASE}/ua/{slug}")
    if not html:
        return None
    m = OG_IMAGE_RE.search(html)
    return m.group(1) if m else None


def get_product_ids_for_group(slug: str, first_page_html: str | None = None) -> set[str]:
    """Проходить усі сторінки пагінації групи і збирає ID товарів.

    Якщо перша сторінка вже була завантажена раніше (наприклад, під час
    discover_children()) - можна передати її в first_page_html, щоб не
    качати той самий URL двічі.
    """
    ids: set[str] = set()
    for page in range(1, MAX_PAGES_PER_GROUP + 1):
        url = f"{BASE}/ua/{slug}" if page == 1 else f"{BASE}/ua/{slug}?page={page}"
        if page == 1 and first_page_html is not None:
            html = first_page_html
        else:
            html = fetch(url)
            time.sleep(DELAY)
        if not html:
            break
        found = set(PRODUCT_RE.findall(html))
        new_ids = found - ids
        if not new_ids:
            # нових товарів на цій сторінці немає -> дійшли до кінця пагінації
            break
        ids |= new_ids
        log.info("  %s стор.%d: +%d товарів (всього %d)", slug, page, len(new_ids), len(ids))
    return ids


def _slug_from_url(url: str) -> str | None:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return parts[-1] if parts else None


def discover_children(slug: str, html: str, known_slugs: set[str]) -> list[tuple[str, str]]:
    """Повертає [(назва, slug), ...] для ПОСИЛАНЬ-НА-ГРУПИ, знайдених на
    сторінці group_url(slug), яких ще немає в known_slugs.

    known_slugs має містити slug'и ВСЬОГО дерева, відомого станом на зараз
    (і статичного TREE, і вже знайдених рекурсією раніше) - сайдбар-меню
    повторюється на кожній сторінці і завжди містить ці самі known_slugs,
    тож автоматично відфільтровується. Залишок - справжні нові підкатегорії
    саме цієї сторінки (як плитки "35*35 Дніпро" на сторінці "Пакети з
    пластиковою ручкою").
    """
    title_by_slug = dict(GROUP_LINK_WITH_TITLE_RE.findall(html))
    all_slugs = set(GROUP_LINK_RE.findall(html))
    new_slugs = all_slugs - known_slugs - {slug}

    result = []
    for child_slug in sorted(new_slugs):
        name = title_by_slug.get(child_slug)
        if not name:
            # Немає title у посиланні - фолбек: качаємо сторінку дитини і
            # беремо назву з її власного <h1>.
            child_html = fetch(f"{BASE}/ua/{child_slug}")
            time.sleep(DELAY)
            if child_html:
                m = H1_RE.search(child_html)
                name = m.group(1).strip() if m else child_slug
            else:
                name = child_slug
        result.append((name.strip(), child_slug))
    return result


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    else:
        data = {}
    data.setdefault("done_groups", {})       # slug -> [product_ids] (як і раніше)
    data.setdefault("images", {})            # slug -> image_url (як і раніше)
    data.setdefault("children_checked", [])  # НОВЕ: slug'и, для яких вже пройшло виявлення дітей
    data.setdefault("discovered", [])        # НОВЕ: [[name, slug, parent_slug], ...] знайдені рекурсією
    return data


def save_progress(progress: dict):
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# Self-healing "сиріт" з prom_woo_sync.py (див. коментар угорі файлу)
# ============================================================

def _extract_breadcrumb_with_slugs(html: str) -> list[tuple[str, str | None]]:
    """Витягує [(назва, slug), ...] з JSON-LD BreadcrumbList сторінки товару,
    без останнього елемента (сам товар, не група)."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "BreadcrumbList":
                items = sorted(d.get("itemListElement", []), key=lambda x: x.get("position", 0))
                result = []
                for it in items:
                    name = it.get("name") or (it.get("item") or {}).get("name")
                    url = it.get("item") if isinstance(it.get("item"), str) else (it.get("item") or {}).get("@id")
                    if name and url:
                        result.append((name.strip(), _slug_from_url(url)))
                return result[:-1]
    return []


def process_pending_orphans(progress: dict, known_slugs: set[str]) -> None:
    """Читає pending_orphans.json (записаний prom_woo_sync.py). Для кожної
    "сироти" дивиться, чи весь її шлях категорій вже відомий known_slugs:
      - якщо так, але прогрес застарів (done_groups) - скидаємо його, щоб
        групу пересканували в цьому ж запуску;
      - якщо в шляху є щось справді нове - воно й так буде знайдено
        основною рекурсією нижче, тут нічого додатково робити не треба.
    """
    if not ORPHANS_FILE.exists():
        return
    orphans = json.loads(ORPHANS_FILE.read_text(encoding="utf-8"))
    if not orphans:
        return

    log.info("process_pending_orphans: %d сиріт для аналізу", len(orphans))
    reset_count = 0

    for o in orphans:
        url = o.get("link")
        if not url:
            continue
        html = fetch(url)
        time.sleep(DELAY_ORPHAN_CHECK)
        if not html:
            continue
        crumb = _extract_breadcrumb_with_slugs(html)[2:]  # без "Олібра" і "Товари та послуги"

        leaf_slug = crumb[-1][1] if crumb else None
        all_known = all(slug in known_slugs for _, slug in crumb if slug)

        if all_known and leaf_slug and leaf_slug in progress["done_groups"]:
            del progress["done_groups"][leaf_slug]
            reset_count += 1
            log.info("  скинуто прогрес для '%s' (товар %s вже мав повний шлях у дереві)", leaf_slug, o["id"])

    if reset_count:
        save_progress(progress)
        log.info("process_pending_orphans: скинуто прогрес для %d груп - будуть пересканован\u0456 в цьому ж запуску", reset_count)


def main():
    progress = load_progress()
    done_groups = progress["done_groups"]
    images = progress["images"]
    children_checked = set(progress["children_checked"])
    discovered = [tuple(x) for x in progress["discovered"]]  # [(name, slug, parent_slug), ...]

    known_slugs = {slug for _, slug, _ in TREE} | {slug for _, slug, _ in discovered}

    # Self-healing застарілого прогресу для сиріт з prom_woo_sync.py (перед
    # основним обходом, щоб скинуті групи одразу пересканувались нижче).
    process_pending_orphans(progress, known_slugs)

    # ------------------------------------------------------------
    # КРОК 0 (НОВЕ): рекурсивне виявлення підкатегорій. Обходимо чергою всі
    # групи з TREE + все, що вже знайдено раніше (на випадок перерваного
    # попереднього запуску), для кожної шукаємо нові дитячі групи і додаємо
    # їх у той самий discovered-список та в чергу - так рекурсія природно
    # йде вглиб, поки не закінчаться нові знахідки.
    # ------------------------------------------------------------
    queue: list[tuple[str, str, str | None]] = list(TREE) + discovered
    first_page_cache: dict[str, str] = {}  # slug -> html першої сторінки, щоб не качати вдруге для товарів

    processed_in_queue = 0
    while queue:
        name, slug, parent = queue.pop(0)
        if slug in children_checked:
            continue
        if len(known_slugs) > MAX_DISCOVERED_NODES:
            log.warning(
                "Досягнуто запобіжника MAX_DISCOVERED_NODES=%d - зупиняю рекурсивне виявлення, "
                "перевір вручну, чи не зациклилось на щось стороннє (типу 'схожі товари').",
                MAX_DISCOVERED_NODES,
            )
            break

        html = fetch(f"{BASE}/ua/{slug}")
        time.sleep(DELAY)
        if html:
            first_page_cache[slug] = html
            new_children = discover_children(slug, html, known_slugs)
            for child_name, child_slug in new_children:
                entry = (child_name, child_slug, slug)
                discovered.append(entry)
                known_slugs.add(child_slug)
                queue.append(entry)
                log.info("  знайдено нову підкатегорію: '%s' (%s), батько '%s'", child_name, child_slug, slug)

        children_checked.add(slug)
        processed_in_queue += 1
        if processed_in_queue % 10 == 0:
            progress["children_checked"] = sorted(children_checked)
            progress["discovered"] = [list(x) for x in discovered]
            save_progress(progress)

    progress["children_checked"] = sorted(children_checked)
    progress["discovered"] = [list(x) for x in discovered]
    save_progress(progress)
    log.info(
        "Рекурсивне виявлення завершено: знайдено %d нових підкатегорій (понад стартові %d з TREE)",
        len(discovered), len(TREE),
    )

    # ------------------------------------------------------------
    # Повне дерево = стартовий TREE + все знайдене рекурсією.
    # ------------------------------------------------------------
    FULL_TREE = list(TREE) + discovered

    parent_slugs = {parent for _, _, parent in FULL_TREE if parent}
    leaves = [(n, s, p) for n, s, p in FULL_TREE if s not in parent_slugs]
    parents_with_page = [(n, s, p) for n, s, p in FULL_TREE if s in parent_slugs]

    # ФІКС (як і раніше): батьківські групи теж можуть містити товари напряму,
    # не лише в підгрупах - тому скануємо і листові, і батьківські групи,
    # листові мають пріоритет (призначаються першими).
    scan_order = leaves + parents_with_page

    log.info(
        "Всього груп: %d (листових: %d, батьківських зі своєю сторінкою: %d)",
        len(FULL_TREE), len(leaves), len(parents_with_page),
    )

    for name, slug, parent in FULL_TREE:
        if not images.get(slug):
            log.info("Фото групи: %s", name)
            img = get_group_image(slug)
            images[slug] = img or ""
            save_progress(progress)
            time.sleep(DELAY)

    for name, slug, parent in scan_order:
        if slug in done_groups:
            log.info("Пропускаю (вже зроблено): %s", name)
            continue
        log.info("Товари групи: %s (%s)", name, slug)
        ids = get_product_ids_for_group(slug, first_page_html=first_page_cache.get(slug))
        done_groups[slug] = sorted(ids)
        save_progress(progress)
        log.info("  -> %d товарів знайдено", len(ids))

    # ---- Формуємо фінальні файли ----
    categories_out = [
        {"name": name, "slug": slug, "parent_slug": parent, "image": images.get(slug, "")}
        for name, slug, parent in FULL_TREE
    ]
    CATEGORIES_FILE.write_text(json.dumps(categories_out, ensure_ascii=False, indent=2), encoding="utf-8")

    product_map = {}
    for name, slug, parent in scan_order:
        for pid in done_groups.get(slug, []):
            if pid not in product_map:
                product_map[pid] = slug
    PRODUCT_MAP_FILE.write_text(json.dumps(product_map, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("Готово. Категорій: %d, товарів у мапі: %d", len(categories_out), len(product_map))
    log.info("Файли: %s, %s", CATEGORIES_FILE, PRODUCT_MAP_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Перервано користувачем - прогрес збережено, можна запустити знову.")
        sys.exit(1)