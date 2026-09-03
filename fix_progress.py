import json
p = json.load(open('/opt/prom-sync/olibra_scrape_progress.json'))
before = len(p.get('discovered', []))
p['discovered'] = [row for row in p.get('discovered', []) if '/' not in row[1] and not row[1].endswith('page_2') and not row[1].endswith('page_3')]
after = len(p['discovered'])
print(f'discovered: {before} -> {after} (видалено {before - after} сміття)')
p['children_checked'] = []
print('children_checked скинуто - рекурсія пройде заново вже з виправленим регексом')
json.dump(p, open('/opt/prom-sync/olibra_scrape_progress.json', 'w'), ensure_ascii=False, indent=2)