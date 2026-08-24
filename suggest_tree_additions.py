import json, re, time, urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from olibra_categories_scraper import TREE, BASE  # беремо реальний TREE напряму з вашого скрапера

FEED_URL = 'https://olibra.com.ua/google_merchant_center.xml?hash_tag=0a95d89def2cfec362187b79040d518f&product_ids=&label_ids=&export_lang=uk&group_ids='
HEADERS = {'User-Agent': 'Mozilla/5.0'}
NS = {'g': 'http://base.google.com/ns/1.0'}

known_slugs = {slug for _, slug, _ in TREE}
name_by_slug = {slug: name for name, slug, _ in TREE}

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def build_id_to_link():
    data = fetch(FEED_URL)
    root = ET.fromstring(data)
    return {
        item.find('g:id', NS).text: item.find('g:link', NS).text
        for item in root.iter('item')
        if item.find('g:id', NS) is not None and item.find('g:link', NS) is not None
    }

def slug_from_url(url):
    # /ua/g4550966-paket-majka  ->  g4550966-paket-majka
    path = urlparse(url).path.strip('/')
    parts = path.split('/')
    return parts[-1] if parts else None

def extract_breadcrumb_with_urls(html):
    """Повертає список (name, slug) для кожного рівня breadcrumb, крім
    самого товару (останній елемент - завжди сам товар, не група)."""
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if isinstance(d, dict) and d.get('@type') == 'BreadcrumbList':
                items = sorted(d.get('itemListElement', []), key=lambda x: x.get('position', 0))
                result = []
                for it in items:
                    name = it.get('name') or (it.get('item') or {}).get('name')
                    url = it.get('item') if isinstance(it.get('item'), str) else (it.get('item') or {}).get('@id')
                    if name and url:
                        result.append((name.strip(), slug_from_url(url)))
                return result[:-1]  # відкидаємо останній - це сам товар, не група
    return []

def main():
    orphan_ids = json.load(open('orphan_ids.json'))
    id_to_link = build_id_to_link()

    suggestions = {}  # slug -> (name, slug, parent_slug)
    already_ok = []    # orphans, чий шлях повністю є в TREE (проблема прогресу, не TREE)

    for i, pid in enumerate(orphan_ids, 1):
        url = id_to_link.get(pid)
        if not url:
            continue
        try:
            html = fetch(url).decode('utf-8', errors='ignore')
        except Exception as e:
            print(f'{pid}: помилка запиту {e}')
            continue

        crumb = extract_breadcrumb_with_urls(html)
        # прибираємо перші 2 рівні - назва сайту і "Товари та послуги" (не реальні групи)
        crumb = crumb[2:]

        parent_slug = None
        new_here = False
        for name, slug in crumb:
            if not slug:
                continue
            if slug not in known_slugs:
                suggestions[slug] = (name, slug, parent_slug)
                known_slugs.add(slug)  # щоб одразу бачити ланцюжок правильно у наступних ітераціях
                new_here = True
            parent_slug = slug

        if not new_here:
            already_ok.append((pid, crumb[-1][1] if crumb else None))

        time.sleep(0.3)
        print(f'[{i}/{len(orphan_ids)}] {pid}: {"є нові рівні" if new_here else "TREE вже повний -> проблема прогресу"}')

    print('\n===== Рядки для додавання в TREE =====')
    for name, slug, parent in suggestions.values():
        parent_repr = f'"{parent}"' if parent else 'None'
        print(f'    ("{name}", "{slug}", {parent_repr}),')

    print('\n===== Ці ID вже мають повний шлях у TREE (проблема прогресу, не TREE) =====')
    for pid, leaf_slug in already_ok:
        print(f'  {pid} -> лист "{leaf_slug}" ({name_by_slug.get(leaf_slug, "?")}) - треба скинути з progress і пересканувати')

if __name__ == '__main__':
    main()