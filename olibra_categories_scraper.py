#!/usr/bin/env python3
"""
Зіставляє товари з правильними категоріями (групами) сайту партнера
olibra.com.ua, а не із загальною таксономією Prom.ua (яку віддає фід).

Чому це потрібно:
Google Merchant фід партнера (product_type) відображає ЗАГАЛЬНУ таксономію
Prom.ua, а не власну структуру груп на сайті партнера. Цей скрипт натомість
проходить по реальних сторінках груп сайту (olibra.com.ua) і будує точну
відповідність: товар -> група, а також забирає фото кожної групи.

Результат зберігається у two файли:
  - olibra_categories.json     - дерево категорій (назва, slug, батько, фото)
  - olibra_product_map.json    - {product_id: category_slug} для кожного товару

Потім prom_woo_sync.py читає ці файли, щоб призначати товарам правильні
категорії (замість product_type з фіда) і виставляти фото категоріям.

Запуск:
    python3 olibra_categories_scraper.py

Це довгий процес (сотні запитів до сайту партнера з паузами, щоб не
перевантажувати їхній сервер) - очікуй 10-20+ хвилин. Прогрес зберігається
по ходу, тому скрипт можна перервати і запустити знову - вже оброблені
групи не будуть оброблятись повторно.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://olibra.com.ua"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CategoryMapBot/1.0)"}
DELAY = 1.0  # пауза між запитами (секунди) - ввічливо до сервера партнера
MAX_PAGES_PER_GROUP = 60  # запобіжник від нескінченного циклу пагінації

OUT_DIR = Path(__file__).resolve().parent
CATEGORIES_FILE = OUT_DIR / "olibra_categories.json"
PRODUCT_MAP_FILE = OUT_DIR / "olibra_product_map.json"
PROGRESS_FILE = OUT_DIR / "olibra_scrape_progress.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("olibra_scraper")

# ============================================================
# Дерево категорій сайту партнера (зібране з навігації сайту).
# Формат: (Назва, slug групи, slug батьківської групи або None)
# Онови цей список вручну, якщо партнер додасть нові групи товарів -
# структура міняється рідко, тому періодичний ручний перегляд є ок.
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

PRODUCT_RE = re.compile(r'href="https://olibra\.com\.ua/ua/p(\d+)-[^"]*\.html"')
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')


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


def get_product_ids_for_group(slug: str) -> set[str]:
    """Проходить усі сторінки пагінації групи і збирає ID товарів."""
    ids: set[str] = set()
    for page in range(1, MAX_PAGES_PER_GROUP + 1):
        url = f"{BASE}/ua/{slug}" if page == 1 else f"{BASE}/ua/{slug}?page={page}"
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


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {"done_groups": {}, "images": {}}


def save_progress(progress: dict):
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    progress = load_progress()
    done_groups = progress["done_groups"]  # slug -> [product_ids]
    images = progress["images"]  # slug -> image_url

    parent_slugs = {parent for _, _, parent in TREE if parent}
    leaves = [(name, slug, parent) for name, slug, parent in TREE if slug not in parent_slugs]

    log.info("Всього груп: %d, з них листових (де є товари): %d", len(TREE), len(leaves))

    for name, slug, parent in TREE:
        if slug not in images:
            log.info("Фото групи: %s", name)
            img = get_group_image(slug)
            images[slug] = img or ""
            save_progress(progress)
            time.sleep(DELAY)

    for name, slug, parent in leaves:
        if slug in done_groups:
            log.info("Пропускаю (вже зроблено): %s", name)
            continue
        log.info("Товари групи: %s (%s)", name, slug)
        ids = get_product_ids_for_group(slug)
        done_groups[slug] = sorted(ids)
        save_progress(progress)
        log.info("  -> %d товарів знайдено", len(ids))

    # ---- Формуємо фінальні файли ----
    categories_out = [
        {"name": name, "slug": slug, "parent_slug": parent, "image": images.get(slug, "")}
        for name, slug, parent in TREE
    ]
    CATEGORIES_FILE.write_text(json.dumps(categories_out, ensure_ascii=False, indent=2), encoding="utf-8")

    product_map = {}
    for slug, ids in done_groups.items():
        for pid in ids:
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
