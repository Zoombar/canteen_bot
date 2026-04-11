# Бот заказа питания (Telegram)

Python 3.10+, **aiogram 3**, **SQLite3**, **APScheduler**. Меню из `.docx` (почта IMAP или ручная загрузка), рассылка меню в будни в заданное время (по умолчанию 8:30), заказ до дедлайна, сводка для столовой (Excel/CSV/текст), месячный отчёт (2 колонки: сотрудник, сумма).

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните `BOT_TOKEN`, `ADMIN_IDS`. Для отправки сводки в столовую укажите `CANTEEN_CHAT_ID` (id пользователя или группы).
2. Установите зависимости:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3. Запуск:

```bash
python -m src.main
```

## Админ-команды (в боте)

- `/add_employee Фамилия Имя` — как при привязке сотрудника в боте.
- `/list_employees`, `/unlink_employee Фамилия Имя`, `/deactivate_employee Фамилия Имя`
- Кнопки: загрузка меню (`.docx`), сводка столовой, внеочередной месячный отчёт.

### Режим тестирования (`TEST_MODE=true` в `.env`)

Включает: заказы **в выходные**, заказ **в любое время** (без дедлайна), а также команды **`/test_*`** для админов (рассылка меню, имитация «закрыто» и т.д.). В бою держите `TEST_MODE=false`.

- `/test_menu`, `/test_menu_me`, `/test_closed`, `/test_open`, `/test_weekday_on`, `/test_weekday_off`, `/test_reset` — работают только при `TEST_MODE=true`.

## Тесты

```bash
pytest -q
```
