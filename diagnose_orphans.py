import json, re, csv, time, urllib.request
from html import unescape

HEADERS = {'User-Agent': 'Mozilla/5.0'}
BASE = 'https://olibra.com.ua/ua/p{}.html'   # сайти на Prom-движку зазвичай коректно
                                              # відкривають сторінку і без правильного slug

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='ignore'), resp.geturl()

def extract_breadcrumb(html):
    # 1) Пробуємо schema.org JSON-LD BreadcrumbList
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
    # 2) Фолбек: шукаємо блок з класом breadcrumbs і витягуємо текст посилань
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
    rows = []
    for i, pid in enumerate(orphan_ids, 1):
        url = BASE.format(pid)
        try:
            html, final_url = fetch(url)
            title = get_title(html)
            crumb = extract_breadcrumb(html)
            print(f'[{i}/{len(orphan_ids)}] {pid}: {title} -> {" > ".join(crumb)}')
            rows.append({'id': pid, 'title': title, 'breadcrumb': ' > '.join(crumb), 'url': final_url})
        except Exception as e:
            print(f'[{i}/{len(orphan_ids)}] {pid}: ПОМИЛКА {e}')
            rows.append({'id': pid, 'title': '', 'breadcrumb': f'ERROR: {e}', 'url': url})
        time.sleep(0.5)  # не бомбити сайт партнера запитами

    with open('orphan_report.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'title', 'breadcrumb', 'url'])
        w.writeheader()
        w.writerows(rows)
    print('\nГотово. Результат у orphan_report.csv')

if __name__ == '__main__':
    main()