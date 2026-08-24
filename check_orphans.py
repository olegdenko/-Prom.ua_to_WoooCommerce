import json, urllib.request, xml.etree.ElementTree as ET 
feed_url = 'https://olibra.com.ua/google_merchant_center.xml?hash_tag=0a95d89def2cfec362187b79040d518f&product_ids=&label_ids=&export_lang=uk&group_ids='
req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
print('Завантажую фід...')
with urllib.request.urlopen(req, timeout=30) as resp:
   data = resp.read() 
   print('Отримано байтів:', len(data)) 
   root = ET.fromstring(data) 
   feed_ids = {el.text for el in root.iter('{http://base.google.com/ns/1.0}id')} 
   print('ID у фіді:', len(feed_ids)) 
   map_ids = set(json.load(open('olibra_product_map.json')).keys()) 
   print('ID в мапі партнера:', len(map_ids)) 
   orphans = feed_ids - map_ids 
   print(len(orphans), 'orphans з', len(feed_ids)) 
   json.dump(sorted(orphans), open('orphan_ids.json', 'w'), ensure_ascii=False, indent=2) 
   print('Збережено в orphan_ids.json') 
   # Зміни від попереднього варіанта: додано timeout=30 (щоб не висіло назавжди,
   # а чітко падало з TimeoutError), додано User-Agent (без нього деякі сайти
   # мовчки ігнорують запит від python-urllib), і print()-и на кожному етапі,
   # щоб бачити, на якому саме кроці зависає, якщо зависне знову.