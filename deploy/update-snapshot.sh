#!/bin/sh
# Подтягивает свежий снимок бота из ветки data (его кладёт ежедневный запуск канала).
# cron пользователя parkan:  */30 * * * * /srv/parking/deploy/update-snapshot.sh
set -e
cd /srv/parking-data
git pull --quiet --ff-only
