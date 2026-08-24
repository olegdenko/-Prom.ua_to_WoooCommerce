import json, xml.etree.ElementTree as ET, urllib.request
feed_url = 'https://olibra.com.ua/google_merchant_center.xml?hash_tag=0a95d89def2cfec362187b79040d518f&product_ids=&label_ids=&export_lang=uk&group_ids='
data = urllib.request.urlopen(feed_url).read()
root = ET.fromstring(data)
ns = {'g': 'http://base.google.com/ns/1.0'}
feed_ids = {el.text for el in root.iter('{http://base.google.com/ns/1.0}id')}
map_ids = set(json.load(open('olibra_product_map.json')).keys())
orphans = feed_ids - map_ids
print(len(orphans), 'orphans з', len(feed_ids))
json.dump(sorted(orphans), open('orphan_ids.json', 'w'), ensure_ascii=False, indent=2)
