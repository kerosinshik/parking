# Бот-дорвей канала «Попутный Метр»

Код — `parkan/channel/bot.py`, команды `channel-export` и `channel-bot`, файлы для сервера — `deploy/`.
Решения и состояние — в [channel.md](channel.md).

## Зачем

Каналы в глобальном поиске Telegram находятся плохо, а боты — по имени, @username и описанию. Бот ловит
запросы вроде «торги Москва», «машино-место», даёт человеку пользу и ведёт в канал. Выше в поиске оказываются
боты, которыми пользуются, причём возвращаются к ним снова. Поэтому у бота три повода зайти ещё раз:

| Что | Без подписки на канал | С подпиской |
|---|---|---|
| Подборка лотов дешевле района (машино-места, квартиры) | 2 лучших лота | 5 лотов, выбор округа |
| Карточка лота по ссылке `t.me/<бот>?start=lot_<id>` | приглашение подписаться | карточка + «похожие» |
| Уведомления о новых лотах по типу и округу | приглашение подписаться | бот пишет сам после обновления снимка |
| Игра «Угадай цену» на настоящих итогах торгов (счёт, серия) | открыта всем | открыта всем |

Игра открыта всем специально: она удерживает и даёт активность, а приглашение в канал появляется в ней
раз в три раунда, а не на каждом шаге. Если подписку проверить нельзя (бот не администратор канала),
бот пускает всех и пишет об этом в журнал.

Ссылки с меткой: `t.me/<бот>?start=vc` — источник запоминается при первом входе, `/stats` показывает,
откуда пришли и сколько из них подписались. Метки для статьи на vc.ru, чатов ЖК, взаимопиара — своя на каждый
канал продвижения. `?start=game` сразу открывает игру.

Команды: `/start`, `/parking`, `/flats`, `/game`, `/alerts`, `/channel`; `/stats` — только администраторам.

## Боевая установка (13.10.2026)

Бот развёрнут автором на своём VPS вручную, не через `install.sh`, поэтому пути отличаются от примеров ниже.
IP сервера, SSH-логины и id администратора здесь не пишем: репозиторий публичный. Они хранятся у автора.

| | |
|---|---|
| Пользователь сервиса | `user-bot` (sudo-операции — под отдельным пользователем автора) |
| Код | `/home/user-bot/torgi_moskva`, ветка `claude/exciting-wright-7y44ub` (в `main` пока не влита) |
| Снимок лотов | `/home/user-bot/torgi_moskva-data/channel_state/bot/lots.json` (клон ветки `data`) |
| База бота (SQLite) | `/home/user-bot/torgi_moskva/bot.sqlite` |
| Служба | `poputny-bot` (systemd) |

Служба:

```bash
sudo systemctl status poputny-bot
journalctl -u poputny-bot -f
sudo systemctl restart poputny-bot
```

Обновить код:

```bash
cd /home/user-bot/torgi_moskva
git pull origin claude/exciting-wright-7y44ub
.venv/bin/pip install -q -e .
sudo systemctl restart poputny-bot
```

Обновить снимок. `channel-export` запускается там, где есть журнал Пульса и итоги торгов (облачный запуск канала),
затем результат пушится в ветку `data`:

```bash
parkan channel-export --state-dir <журнал> \
    --results results/torgi-mashinomesta.csv,results/torgi-kvartiry.csv \
    --out channel_state/bot/lots.json
```

На сервере после этого достаточно `git -C /home/user-bot/torgi_moskva-data pull -q --ff-only`. Перезапуск не нужен:
бот сам замечает новый файл, перечитывает его и рассылает уведомления. Автоматически это делает cron раз
в 30 минут: `deploy/update-snapshot.sh`, путь `cd` в нём поправить на `/home/user-bot/torgi_moskva-data`.

`.env` на сервере:

```
PARKAN_BOT_TOKEN=<токен от @BotFather>
PARKAN_CHANNEL_URL=https://t.me/poputny_metr
PARKAN_BOT_ADMINS=<id автора в Telegram>
PARKAN_BOT_LOTS=/home/user-bot/torgi_moskva-data/channel_state/bot/lots.json
PARKAN_BOT_DB=/home/user-bot/torgi_moskva/bot.sqlite
```

Если служба собрана из `deploy/poputny-bot.service`, в ней должны стоять пути `/home/user-bot/...`
и не должно быть `ProtectHome=true`: эта настройка закрывает `/home`, и бот не прочитает ни код, ни снимок.

## Данные

Бот не ходит в портал и не считает медианы. Он читает снимок, который каждый день готовит запуск канала:

```bash
parkan channel-export --state-dir <журнал Пульса> \
    --results results/torgi-mashinomesta.csv,results/torgi-kvartiry.csv \
    --out channel_state/bot/lots.json
```

В снимке лоты в продаже дешевле медианы района или округа хотя бы на 10 % (тот же расчёт, что у канала,
`lots.py`) и итоги торгов за 90 дней для игры. На данных 13.10.2026 вышло 305 лотов, 1 870 итогов, 0,6 МБ.
Снимок лежит в ветке `data`, а сервер забирает его оттуда через `git pull` по cron. Обновился файл —
бот перечитывает его и рассылает уведомления о лотах, которых раньше не было (первый снимок только
запоминается как точка отсчёта).

## Установка на VPS

Быстрый путь — одна команда от root на сервере (Debian/Ubuntu). Она ставит пакеты, создаёт пользователя `parkan`,
код и снимок, спрашивает токен и ваш id, запускает службу и cron. Повторный запуск обновляет код:

```bash
curl -fsSL https://raw.githubusercontent.com/kerosinshik/parking/main/deploy/install.sh | sudo bash
```

Ниже то же самое по шагам, если хочется понимать, что происходит.

Нужны Python 3.10+, git и исходящий доступ к `api.telegram.org` и GitHub. Входящие порты не нужны:
бот опрашивает Telegram сам (long polling).

1. **Бот.** Отдельный от того, что пишет в кухню: у @BotFather `/newbot`. Имя и @username — с ключевыми
   словами, по ним ищут: например, имя «Торги Москвы — машино-места и квартиры», username `torgi_moskva_bot`
   (или свободный похожий). В @BotFather — картинка (`/setuserpic`). Описание и меню бот ставит сам при запуске.
2. **Права в канале.** Добавить бота администратором канала (все права можно выключить): без этого
   Telegram не скажет, подписан ли человек.
3. **Код и данные:**
   ```bash
   sudo useradd -r -m -d /srv/parking -s /bin/bash parkan
   sudo mkdir -p /var/lib/poputny-bot && sudo chown parkan /var/lib/poputny-bot
   sudo -iu parkan
   git clone https://github.com/kerosinshik/parking /srv/parking
   cd /srv/parking && python3 -m venv .venv && .venv/bin/pip install -e .
   # только снимок из ветки data, без журнала Пульса
   git clone --branch data --single-branch --filter=blob:none --sparse \
       https://github.com/kerosinshik/parking /srv/parking-data
   git -C /srv/parking-data sparse-checkout set channel_state/bot
   ```
   Если репозиторий приватный — deploy key только на чтение (GitHub → Settings → Deploy keys) и адрес
   `git@github.com:kerosinshik/parking.git`.
4. **Настройки:** `cp deploy/bot.env.example .env`, вписать токен и свой id (узнать у @userinfobot), `chmod 600 .env`.
5. **Проверка:** `set -a; . ./.env; set +a; .venv/bin/parkan channel-bot --setup-only` — описание и меню обновятся.
6. **Служба и обновление снимка:**
   ```bash
   sudo cp deploy/poputny-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now poputny-bot
   journalctl -u poputny-bot -f                     # журнал
   crontab -e   # от parkan:  */30 * * * * /srv/parking/deploy/update-snapshot.sh
   ```

Обновить код: `cd /srv/parking && git pull && .venv/bin/pip install -e . && sudo systemctl restart poputny-bot`.

База бота (`/var/lib/poputny-bot/bot.sqlite`: пользователи, уведомления, счёт игры) хранится только на сервере,
в репозиторий не попадает. Это персональные данные (id и имя в Telegram), их стоит бэкапить вместе с сервером.

## Переменные

| Переменная | Что это |
|---|---|
| `PARKAN_BOT_TOKEN` | токен бота-дорвея (если не задан — `TELEGRAM_BOT_TOKEN`) |
| `PARKAN_CHANNEL_URL` | ссылка на канал, по умолчанию `https://t.me/poputny_metr`; из неё же — @канал для проверки подписки |
| `PARKAN_CHANNEL_CHAT` | @канал или id для проверки подписки, если ссылка не вида t.me/имя |
| `PARKAN_BOT_ADMINS` | id администраторов через запятую: им `/stats` и всё без подписки |
| `PARKAN_BOT_LOTS` | файл снимка, по умолчанию `channel_state/bot/lots.json` |
| `PARKAN_BOT_DB` | база бота, по умолчанию `bot.sqlite` в рабочей папке |
| `PARKAN_BOT_GATE` | `0` — всё открыто без подписки |
| `PARKAN_CHANNEL_NAME` | название канала в текстах, по умолчанию «Попутный Метр» |

## Что дальше

- Ссылки «подробнее» в постах канала вести в бота (`?start=lot_<id>`) — после того как бот поработает.
- Платный тариф: мгновенные уведомления, снижения цен, нежилые помещения.
- Рассылка всем пользователям — только по делу и редко: за жалобы на спам Telegram ограничивает бота.
