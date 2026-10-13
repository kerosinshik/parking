#!/bin/bash
# Установка и обновление бота-дорвея на VPS (Debian/Ubuntu) одной командой, от root:
#
#   curl -fsSL https://raw.githubusercontent.com/kerosinshik/parking/main/deploy/install.sh | sudo bash
#
# Ветка кода — BRANCH (по умолчанию main):  ... | sudo BRANCH=другая-ветка bash
# Первый запуск спросит токен бота и ваш id в Telegram; повторный — обновит код и перезапустит службу.
set -euo pipefail

REPO=https://github.com/kerosinshik/parking
BRANCH=${BRANCH:-main}
APP=/srv/parking
DATA=/srv/parking-data
VAR=/var/lib/poputny-bot
U=parkan

[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo)"; exit 1; }
say() { echo; echo "==> $*"; }
as() { runuser -u "$U" -- "$@"; }

say "Пакеты"
if command -v apt-get >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

say "Пользователь $U и папки"
id "$U" >/dev/null 2>&1 || useradd -r -m -d "$APP" -s /bin/bash "$U"
mkdir -p "$VAR" && chown "$U" "$VAR" && chmod 700 "$VAR"

say "Код ($BRANCH)"
if [ -d "$APP/.git" ]; then
    as git -C "$APP" fetch -q origin "$BRANCH"
    as git -C "$APP" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
    tmp=$(mktemp -d) && chown "$U" "$tmp"
    as git clone -q --branch "$BRANCH" "$REPO" "$tmp/parking"
    shopt -s dotglob && mv "$tmp/parking"/* "$APP"/ && rm -rf "$tmp"
fi
[ -x "$APP/.venv/bin/python" ] || as python3 -m venv "$APP/.venv"
as "$APP/.venv/bin/pip" install -q --upgrade pip
as "$APP/.venv/bin/pip" install -q -e "$APP[bot]"

say "Chromium для картинок (Playwright; без него бот отвечает текстом)"
"$APP/.venv/bin/python" -m playwright install-deps chromium >/dev/null || echo "   системные библиотеки не поставились — картинок не будет"
as "$APP/.venv/bin/python" -m playwright install chromium >/dev/null || echo "   Chromium не скачался — картинок не будет"

say "Снимок лотов из ветки data (только channel_state/bot)"
if [ ! -d "$DATA/.git" ]; then
    mkdir -p "$DATA" && chown "$U" "$DATA"
    as git clone -q --branch data --single-branch --filter=blob:none --sparse "$REPO" "$DATA"
    as git -C "$DATA" sparse-checkout set channel_state/bot
else
    as git -C "$DATA" pull -q --ff-only
fi
[ -f "$DATA/channel_state/bot/lots.json" ] || echo "   снимка пока нет: бот запустится, подборки появятся после первого channel-export"

say "Настройки $APP/.env"
if [ ! -f "$APP/.env" ]; then
    read -rp "Токен бота от @BotFather: " TOKEN </dev/tty
    read -rp "Ваш числовой id в Telegram (у @userinfobot), для /stats: " ADMIN </dev/tty
    read -rp "Ссылка на канал [https://t.me/poputny_metr]: " URL </dev/tty
    cat > "$APP/.env" <<EOF
PARKAN_BOT_TOKEN=$TOKEN
PARKAN_CHANNEL_URL=${URL:-https://t.me/poputny_metr}
PARKAN_BOT_ADMINS=$ADMIN
PARKAN_BOT_LOTS=$DATA/channel_state/bot/lots.json
PARKAN_BOT_DB=$VAR/bot.sqlite
EOF
    chown "$U" "$APP/.env" && chmod 600 "$APP/.env"
else
    echo "   уже есть, не трогаю"
fi

say "Проверка токена, описание и меню бота"
as bash -c "set -a; . '$APP/.env'; set +a; '$APP/.venv/bin/parkan' channel-bot --setup-only"

say "Служба systemd и обновление снимка по cron"
cp "$APP/deploy/poputny-bot.service" /etc/systemd/system/poputny-bot.service
systemctl daemon-reload
systemctl enable -q poputny-bot
systemctl restart poputny-bot
line="*/30 * * * * $APP/deploy/update-snapshot.sh"
( crontab -u "$U" -l 2>/dev/null | grep -vF update-snapshot.sh; echo "$line" ) | crontab -u "$U" -

sleep 3
systemctl --no-pager --lines=5 status poputny-bot || true
say "Готово. Журнал: journalctl -u poputny-bot -f · обновить: запустить эту же команду ещё раз"
