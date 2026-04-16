# Деплой бота на Ubuntu по SSH (пошаговый гайд)

Этот гайд для запуска `canteen_bot` на Ubuntu-сервере с автозапуском через `systemd` и загрузкой меню через IMAP.

## 1) Подключение к серверу

С локальной машины:

```bash
ssh user@server_ip
```

Если вход по ключу:

```bash
ssh -i ~/.ssh/id_ed25519 user@server_ip
```

## 2) Что установить на сервер

На Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Проверка:

```bash
git --version
python3 --version
```

## 3) Подготовка deploy key для GitHub (рекомендуется)

На сервере:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C "deploy-canteen-bot" -f ~/.ssh/canteen_bot_deploy -N ""
chmod 600 ~/.ssh/canteen_bot_deploy
```

Скопируй публичный ключ:

```bash
cat ~/.ssh/canteen_bot_deploy.pub
```

В GitHub репозитории `Zoombar/canteen_bot`:

- `Settings` -> `Deploy keys` -> `Add deploy key`
- вставь содержимое `.pub`
- `Allow write access` обычно **не нужен** (для деплоя достаточно read-only)

Проверь доступ:

```bash
ssh -i ~/.ssh/canteen_bot_deploy -o IdentitiesOnly=yes -T git@github.com
```

## 4) Клонирование проекта

Пример рабочего пути:

```bash
cd /home/it/canteen_bot
```

Клонирование по SSH:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/canteen_bot_deploy -o IdentitiesOnly=yes" git clone git@github.com:Zoombar/canteen_bot.git .
```

## 5) Python-окружение и зависимости

```bash
cd /home/it/canteen_bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 6) Настройка `.env`

```bash
cd /home/it/canteen_bot
cp .env.example .env
```

Заполни минимум:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `CANTEEN_CHAT_ID`
- `MENU_BROADCAST_TIME` (например `08:30`)
- `ORDER_DEADLINE_TIME` (например `11:00`)

Для IMAP:

- `IMAP_HOST`
- `IMAP_PORT=993`
- `IMAP_USER`
- `IMAP_PASSWORD`
- `IMAP_SENDER_FILTER` (опционально)
- `IMAP_ONLY_UNSEEN=true`

Важно:

- Таймзона в проекте зафиксирована как `Asia/Omsk` в коде.

## 7) Ручной тест запуска

```bash
cd /home/it/canteen_bot
source .venv/bin/activate
python -m src.main
```

Проверь, что бот отвечает в Telegram. Остановить: `Ctrl+C`.

## 8) Автозапуск через systemd

Создай сервис:

```bash
sudo nano /etc/systemd/system/canteen-bot.service
```

Вставь:

```ini
[Unit]
Description=Canteen Bot (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=it
WorkingDirectory=/home/it/canteen_bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/it/canteen_bot/.venv/bin/python -m src.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

В этом гайде используется пользователь `it`.

Применить и запустить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now canteen-bot
sudo systemctl status canteen-bot
```

Логи:

```bash
journalctl -u canteen-bot -f --tail=100
```

## 9) Обновление (деплой новых изменений)

### Вариант A: через deploy.sh (рекомендуется)

```bash
cd /home/it/canteen_bot
DEPLOY_BRANCH=main DEPLOY_KEY_PATH=/home/it/.ssh/canteen_bot_deploy ./deploy.sh
sudo systemctl restart canteen-bot
```

Если нужно очистить build cache Docker (если используешь docker):

```bash
DEPLOY_DOCKER_PRUNE=1 DEPLOY_BRANCH=main DEPLOY_KEY_PATH=/home/it/.ssh/canteen_bot_deploy ./deploy.sh
```

### Вариант B: вручную

```bash
cd /home/it/canteen_bot
GIT_SSH_COMMAND="ssh -i ~/.ssh/canteen_bot_deploy -o IdentitiesOnly=yes" git fetch origin
git checkout main
GIT_SSH_COMMAND="ssh -i ~/.ssh/canteen_bot_deploy -o IdentitiesOnly=yes" git pull --ff-only origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart canteen-bot
```

## 10) Полезные команды эксплуатации

Статус сервиса:

```bash
sudo systemctl status canteen-bot
```

Перезапуск:

```bash
sudo systemctl restart canteen-bot
```

Остановка:

```bash
sudo systemctl stop canteen-bot
```

Последние логи:

```bash
journalctl -u canteen-bot --since "1 hour ago"
```

## 11) Частые проблемы

1. Бот не стартует (`SystemExit: Укажите BOT_TOKEN`)  
   Проверь `.env` и путь `WorkingDirectory` в systemd.

2. Нет доступа к GitHub по SSH  
   Проверь deploy key и команду `ssh -T git@github.com`.

3. Не подтягивается меню из IMAP  
   Проверь `IMAP_*` переменные и доступ к почтовому серверу.

4. Ошибки после обновления зависимостей  
   Выполни `pip install -r requirements.txt` и перезапусти сервис.

5. Бот запустился, но не видит старую БД  
   Проверь путь проекта `/home/it/canteen_bot` и наличие `data/bot.db`.

## 12) Рекомендации

- Держи `TEST_MODE=false` в проде.
- Не коммить `.env` и приватные SSH-ключи.
- Регулярно делай бэкап `data/bot.db`.
- После каждого деплоя проверяй:
  - `sudo systemctl status canteen-bot`
  - `journalctl -u canteen-bot -n 50`
