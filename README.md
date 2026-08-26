# Prom.ua to WooCommerce Sync

Інструмент на Python для автоматичної синхронізації каталогу товарів, категорій та зображень із фіду/вивантаження **Prom.ua** в інтернет-магазин на **WooCommerce**.

---

## 📌 Основні можливості

* **Синхронізація категорій та товарів** — автоматичний імпорт і оновлення структури категорій та карток товарів.

* **Діагностика осиротілих записів** — скрипти для виявлення та аналізу втрачених або застарілих категорій і товарів:

  * `check_orphans.py`
  * `diagnose_orphans.py`

* **Парсинг та обробка медіа** — обробка зображень категорій і товарів.

* **Сповіщення в Telegram** — інтегрована система сповіщень і команд для моніторингу процесу синхронізації в реальному часі.

* **Захист від збоїв** — обробка мережевих помилок та стабілізація підключень.

---

## 🛠 Встановлення та налаштування

### 1. Клонування репозиторію

```bash
git clone https://github.com/olegdenko/-Prom.ua_to_WoooCommerce.git
cd -Prom.ua_to_WoooCommerce
```

### 2. Створення та активація віртуального середовища

```bash
python -m venv .venv
```

**Linux / macOS:**

```bash
source .venv/bin/activate
```

**Windows:**

```bat
.venv\Scripts\activate
```

### 3. Встановлення залежностей

```bash
pip install -r requirements.txt
```

### 4. Налаштування змінних оточення

Скопіюйте шаблонний файл конфігурації:

**Linux / macOS:**

```bash
cp prom_woo_sync.env.example prom_woo_sync.env
```

**Windows:**

```bat
copy prom_woo_sync.env.example prom_woo_sync.env
```

Відкрийте файл `prom_woo_sync.env` та заповніть необхідні параметри:

* URL фідів **Prom.ua**;
* **WooCommerce REST API** — Consumer Key та Consumer Secret;
* токен **Telegram-бота** та Chat ID — за потреби.

> ⚠️ **Не публікуйте файл `prom_woo_sync.env` у GitHub**, оскільки він може містити API-ключі, токени та інші секретні дані.

Файл `prom_woo_sync.env.example` можна зберігати в репозиторії. Він повинен містити лише назви параметрів і приклади значень без реальних ключів та токенів.

---

## 🚀 Використання

### Основна синхронізація

Запуск головного сценарію синхронізації:

```bash
python prom_woo_sync.py
```

### Допоміжні скрипти

#### Перевірка "осиротілих" товарів та категорій

```bash
python check_orphans.py
```

```bash
python diagnose_orphans.py
```

#### Інспекція фідів

```bash
python inspect_feed.py
```

#### Скрейпінг категорій

```bash
python olibra_categories_scraper.py
```

---

## 📁 Основні файли

| Файл                           | Призначення                                         |
| ------------------------------ | --------------------------------------------------- |
| `prom_woo_sync.py`             | Основний скрипт синхронізації Prom.ua → WooCommerce |
| `check_orphans.py`             | Перевірка осиротілих товарів і категорій            |
| `diagnose_orphans.py`          | Детальна діагностика осиротілих записів             |
| `inspect_feed.py`              | Перевірка та аналіз фіду Prom.ua                    |
| `olibra_categories_scraper.py` | Отримання та обробка категорій                      |
| `requirements.txt`             | Залежності Python                                   |
| `prom_woo_sync.env`            | Локальна конфігурація та секретні ключі             |
| `prom_woo_sync.env.example`    | Шаблон конфігурації                                 |

---

## 🔐 Безпека

Не додавайте до репозиторію:

* `prom_woo_sync.env`;
* API-ключі WooCommerce;
* токени Telegram;
* паролі;
* інші секретні дані.

Переконайтеся, що `prom_woo_sync.env` доданий до `.gitignore`:

```gitignore
prom_woo_sync.env
.venv/
__pycache__/
*.pyc
```

---

## 📄 Ліцензія

Цей проєкт розповсюджується під ліцензією **Apache License 2.0**.

Детальніше дивіться у файлі [LICENSE](LICENSE).
