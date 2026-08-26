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

# inspect_feed.py
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = 'https://olibra.com.ua/google_merchant_center.xml?hash_tag=0a95d89def2cfec362187b79040d518f&product_ids=&label_ids=&export_lang=uk&group_ids='
req = urllib.request.Request(FEED_URL, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = resp.read()

root = ET.fromstring(data)
print('Корінний тег:', root.tag)
print('Атрибути кореня:', root.attrib)

# перший рівень дітей кореня
for child in list(root)[:3]:
    print(' дитина:', child.tag)

# знайдемо перший елемент, що містить g:id, і виведемо його теги "як є"
for el in root.iter():
    if el.tag.endswith('}id') or el.tag == 'id':
        parent = None
        # знайдемо батька цього елемента
        for p in root.iter():
            if el in list(p):
                parent = p
                break
        print('\nПерший елемент з g:id всередині тега:', parent.tag)
        print('Його прямі діти (теги):')
        for c in list(parent):
            print('  ', c.tag, '=', (c.text or '')[:60])
        break