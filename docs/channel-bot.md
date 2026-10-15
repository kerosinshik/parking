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

Метки роликов и постов вне Telegram (Instagram и т. п.) — `channel_state/bot/campaigns.json` рядом со снимком,
бот перечитывает его так же, как `lots.json` (приходит в ветку `data` → `git pull` по cron):

```json
{"igr01": {"lot": "20197917", "word": "можайка", "title": "Reels 01: Можайское ш., 45к1", "date": "2026-10-15"}}
```

- `t.me/<бот>?start=igr01` открывает лот кампании; `?start=igr01-lot_<id>` — то же без записи в файле;
- кодовое слово (`word`) — для тех, кто нашёл бота поиском: слово присылают обычным сообщением, регистр, «ё»
  и знаки не важны; бот открывает лот и засчитывает переход метке;
- каждый переход пишется в таблицу `clicks` (время, пользователь, метка, `start`/`word`, новый ли — запустил бота
  не раньше чем за час); источник пользователя — по-прежнему первая метка;
- `/stats` — раздел «Переходы по меткам»: переходов, людей, новых, подписаны на канал, дата последнего.
- Записать метку: `parkan channel-reel --facts <facts.json> --campaigns <data>/channel_state/bot/campaigns.json`
  (поля `campaign`, `lot_id`, `word` в фактах ролика) и закоммитить файл в `data`.

Метки: `igr<NN>` — Reels, `igs<NN>` — Stories, `igbio` — ссылка в профиле, `ig<NN>` — пост-карусель.
Латиница, цифры, `_` и `-`, до 64 знаков (ограничение Telegram).

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
.venv/bin/pip install -q -e '.[bot]'
sudo systemctl restart poputny-bot
```

Картинки (один раз). Chromium от Playwright, а не системный: на Ubuntu системный ставится через snap,
а snap-версия не видит временные файлы службы.

```bash
cd /home/user-bot/torgi_moskva
.venv/bin/pip install -q -e '.[bot]'                                          # под user-bot
sudo /home/user-bot/torgi_moskva/.venv/bin/python -m playwright install-deps chromium   # под пользователем с sudo
.venv/bin/python -m playwright install chromium                               # под user-bot
sudo systemctl restart poputny-bot
journalctl -u poputny-bot -n 30 --no-pager | grep -i картинк                  # «Картинки: рисую в фоне…»
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

## Картинки

Те же карточки, что в канале (`html_cards`, макеты Claude Design): обложка в приветствии, подборка списком
с картой районов, карточка лота, вопрос игры (старт и место видны, итог нет). Код — `parkan/channel/bot_media.py`.

Холодный старт Chromium занимает 1–5 с, а бот должен отвечать сразу, поэтому картинку бот никогда не ждёт.
Если карточка готова, уходит фото с подписью (длинный текст — следом отдельным сообщением). Если нет, ответ
идёт текстом, а карточка рисуется вне очереди и в следующий раз будет картинкой. После обновления снимка
фоновый поток заранее рисует около 180 карточек: обложку, подборки по Москве и округам, 20 лучших лотов
каждого типа, 100 вопросов игры. На машине уровня VPS это занимает около 4 минут. Игра выбирает вопросы
с готовой картинкой, когда их набралось хотя бы пять. Отправленный файл запоминается по `file_id` Telegram
и повторно не загружается. Карточки старше 3 дней удаляются.

Нужны Pillow и Chromium (`pip install -e '.[bot]'`, `python -m playwright install chromium`). Без них бот
работает текстом, в журнале пишет «Картинки: выключены». Папка — `PARKAN_BOT_CARDS` (по умолчанию `cards/`
рядом с базой), `PARKAN_BOT_CARDS=0` выключает картинки. `/stats` показывает, сколько нарисовано и сколько в очереди.

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

## Обновление лотов

Цепочка: расписание «Попутный Метр — черновики дня» (облако, 7:20 МСК) → `channel-export` → коммит
`channel_state/bot/lots.json` в ветку `data` → cron на VPS раз в 30 минут делает `git pull` → бот замечает
новый файл, рассылает уведомления и рисует карточки. Перезапускать бота не нужно.

Шаг для расписания вставляется после шага 2. Добавить его можно только из сессии самого расписания:

> 2а. Снимок для бота-дорвея (docs/channel-bot.md): если команда есть (`python3 -m parkan.cli channel-export --help`
> завершается с кодом 0), выполни `python3 -m parkan.cli channel-export --state-dir /home/user/parking-data/state
> --results /home/user/parking-data/results/torgi-mashinomesta.csv,/home/user/parking-data/results/torgi-kvartiry.csv
> --out /home/user/parking-data/channel_state/bot/lots.json`. Команды нет — пропусти шаг и упомяни в итоге. Сбой шага
> не прерывает остальные; файл коммитится в шаге 6 вместе с channel_state/. В итог добавь строку channel-export.

Расписание работает с `main`, поэтому шаг заработает после слияния ветки бота. До этого он пропускается.

Cron на VPS (под `user-bot`, `crontab -e`):

```
*/30 * * * * git -C /home/user-bot/torgi_moskva-data pull -q --ff-only
```

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
| `PARKAN_BOT_CARDS` | папка картинок (по умолчанию `cards/` рядом с базой); `0` — без картинок |
| `PARKAN_CHANNEL_NAME` | название канала в текстах, по умолчанию «Попутный Метр» |

## Что дальше

- Ссылки «подробнее» в постах канала вести в бота (`?start=lot_<id>`) — после того как бот поработает.
- Платный тариф: мгновенные уведомления, снижения цен, нежилые помещения.
- Рассылка всем пользователям — только по делу и редко: за жалобы на спам Telegram ограничивает бота.
