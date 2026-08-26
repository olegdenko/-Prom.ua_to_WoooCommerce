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

import json, re, csv, time, urllib.request
import xml.etree.ElementTree as ET
from html import unescape

FEED_URL = 'https://olibra.com.ua/google_merchant_center.xml?hash_tag=0a95d89def2cfec362187b79040d518f&product_ids=&label_ids=&export_lang=uk&group_ids='
HEADERS = {'User-Agent': 'Mozilla/5.0'}
NS = {'g': 'http://base.google.com/ns/1.0'}

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def build_id_to_link():
    """Читає фід і повертає {id: link} для кожного товару, беручи
    посилання прямо з фіда - без вгадування URL-патерну."""
    data = fetch(FEED_URL)
    root = ET.fromstring(data)
    id_to_link = {}
    # шукаємо всі <item>, у кожного беремо g:id і link (не обов'язково з g:)
    for item in root.iter('item'):
        gid = item.find('g:id', NS)
        link = item.find('g:link', NS)
        if gid is not None and link is not None and gid.text and link.text:
            id_to_link[gid.text] = link.text
    return id_to_link

def extract_breadcrumb(html):
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if isinstance(d, dict) and d.get('@type') == 'BreadcrumbList':
                items = sorted(d.get('itemListElement', []), key=lambda x: x.get('position', 0))
                names = [it.get('name') or (it.get('item') or {}).get('name') for it in items]
                names = [n for n in names if n]
                if names:
                    return names
    m = re.search(r'<[^>]+class="[^"]*breadcrumb[^"]*"[^>]*>(.*?)</(?:nav|div|ul)>', html, re.S | re.I)
    if m:
        links = re.findall(r'>([^<]{2,60})</a>', m.group(1))
        return [unescape(t.strip()) for t in links if t.strip()]
    return []

def get_title(html):
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    if m:
        return unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())
    return ''

def main():
    orphan_ids = json.load(open('orphan_ids.json'))
    print('Читаю фід, щоб отримати точні посилання...')
    id_to_link = build_id_to_link()
    print(f'У фіді знайдено {len(id_to_link)} пар id->link')

    rows = []
    for i, pid in enumerate(orphan_ids, 1):
        url = id_to_link.get(pid)
        if not url:
            print(f'[{i}/{len(orphan_ids)}] {pid}: НЕМА посилання в фіді (дивно, перевір вручну)')
            rows.append({'id': pid, 'title': '', 'breadcrumb': 'NO LINK IN FEED', 'url': ''})
            continue
        try:
            html = fetch(url).decode('utf-8', errors='ignore')
            title = get_title(html)
            crumb = extract_breadcrumb(html)
            print(f'[{i}/{len(orphan_ids)}] {pid}: {title} -> {" > ".join(crumb)}')
            rows.append({'id': pid, 'title': title, 'breadcrumb': ' > '.join(crumb), 'url': url})
        except Exception as e:
            print(f'[{i}/{len(orphan_ids)}] {pid}: ПОМИЛКА {e} (url={url})')
            rows.append({'id': pid, 'title': '', 'breadcrumb': f'ERROR: {e}', 'url': url})
        time.sleep(0.5)

    with open('orphan_report.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'title', 'breadcrumb', 'url'])
        w.writeheader()
        w.writerows(rows)
    print('\nГотово. Результат у orphan_report.csv')

if __name__ == '__main__':
    main()