# parkan — аналитика загрузки парковок и нарушений в Москве

MVP по исследованию «Реализуемость аналитики парковочной загрузки и нарушений в Москве».
Инструмент собирает справочник парковок с портала открытых данных (набор № 623),
принимает снимки занятости (occupancy) и обезличенные события нарушений из любых
разрешённых источников, сопоставляет их по месту и времени и строит почасовую
аналитику по районам с HTML-дашбордом.

Связь загрузки и нарушений везде показывается как **корреляция, а не причинность**.

## Что работает сейчас и чего ждём от города

| Компонент | Статус |
|---|---|
| Справочник парковок (набор № 623, `apidata.mos.ru`) и история его версий | готово, нужен ключ API |
| Импорт нарушений (CSV/JSON), защита от персональных данных | готово, ждёт выгрузки от ЦОДД/АМПП/МАДИ |
| Occupancy: ручной файл / официальная выгрузка / опрос официального API | готово; источник появится после соглашения с АМПП/ЦОДД |
| Контекст: перекрытия и погода (CSV) | готово |
| Сопоставление ±15 мин, метрики, витрина, дашборд, экспорт в Parquet | готово |

Без официальных потоков occupancy и нарушений можно построить инфраструктурный и
исторический прототип, но нельзя честно обещать статистику «сколько мест занято сейчас».

## Установка

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                       # 22 теста
```

Нужен Python 3.10+. Единственная зависимость — DuckDB. PostGIS не нужен: геометрия считается в Python.

## Быстрая проверка на демо-данных

```bash
parkan demo --workdir demo_out
open demo_out/report.html
```

`demo` генерирует данные за 14 дней по двум районам (40 сегментов, ~54 тыс. снимков,
~860 нарушений) в тех же форматах, что ожидаются от реальных источников (`demo_out/input/`),
и прогоняет весь конвейер. В данные заложена связь «нарушений больше при загрузке выше 75 %».
Конвейер её находит: в демо r ≈ 0,69 для Арбата.

## Работа с реальными данными

```bash
export PARKAN_DB=data/parkan.duckdb       # по умолчанию
export MOS_API_KEY=...                    # ключ с https://apidata.mos.ru

parkan fetch-623                          # справочник: снимок + история изменений
# или, если API недоступен, — файл, скачанный с data.mos.ru:
parkan load-623 data-623.json

parkan import-occupancy occupancy.csv --source-type official_file --source-name ampp
parkan import-violations violations_2026-01.csv
parkan import-weather weather.csv
parkan import-road-events road_events.csv

parkan build-mart                          # --window 15 --max-distance 100 --near-full 0.9
parkan summary
parkan report --out data/report.html
parkan export --out data/export            # normalized/*.parquet, mart/*.parquet
```

Регулярный сбор (раз в 5–15 минут) — через cron или systemd-таймер, Kafka не нужна:

```cron
*/10 * * * * cd /srv/parking && .venv/bin/parkan collect-occupancy --url "$OCC_URL" --source-name ampp --map "parking_id=id,free_spaces=free,measured_at=ts" >> data/collect.log 2>&1
0 4 * * *    cd /srv/parking && .venv/bin/parkan fetch-623 >> data/collect.log 2>&1
```

Заголовки для официального API (например, токен) передаются через `OCCUPANCY_HEADERS='{"Authorization": "Bearer ..."}'`.

## Форматы входных файлов

CSV с разделителем `,` `;` или табуляцией, кодировка UTF-8 или CP1251, либо JSON-список.
Время без часового пояса считается московским; со смещением (`Z`, `+03:00`) — переводится в Москву.
Принимаются форматы `2026-09-07 12:05`, `2026-09-07T12:05:00Z`, `07.09.2026 12:05`.

**Нарушения** (`import-violations`):

| поле | обяз. | комментарий |
|---|---|---|
| `violation_id_or_anonymized_id` (или `violation_id`) | да | уникален в пределах `source_system` |
| `source_system` | да | МАДИ, ЦОДД, АМПП, Помощник Москвы, камера… |
| `violation_type` | да | |
| `status` | да | сообщение / проверено / постановление / отклонено (или report / verified / ruling / rejected) |
| `occurred_at` | да | время события |
| `recorded_at`, `resolution_at` | | |
| `latitude`, `longitude` | | WGS84; проверяется попадание в Москву |
| `street_segment_id` | | = `parking_id` справочника, если известен |
| `parking_zone_id`, `administrative_district`, `municipality_or_area`, `vehicle_category` | | |

Файл, в котором есть поля вроде госномера, ФИО, фото, телефона или VIN, **не загружается**.
Флаг `--drop-pii` отбрасывает такие поля. Повторный импорт той же записи обновляет её, а не дублирует.

**Occupancy** (`import-occupancy`): `parking_id`, `measured_at` и `occupied_spaces` или `free_spaces`.
`total_spaces` необязателен: если его нет, берётся вместимость из справочника.
Имена полей можно переопределить через `--map "parking_id=id,free_spaces=data.free"`.

**Погода**: `hour_start, temperature_c, precipitation_mm`.
**Дорожные события**: `event_id, event_type, starts_at, ends_at, latitude, longitude, municipality_or_area, description`.
Если `municipality_or_area` пусто, событие считается общегородским.

## Как считается

1. **Сегмент.** Если указан `street_segment_id`, используется он. Иначе берётся ближайший
   сегмент справочника в радиусе `--max-distance` (100 м). Иначе событие не сопоставлено,
   но учитывается в районе, если тот указан в самом событии.
2. **Снимок загрузки.** Ищется ближайший снимок того же сегмента в окне ±`--window` минут.
   Снимок до события предпочтительнее.
3. **Метрики** (`hourly_area_metrics`, район × час): `occupancy_rate`, `violations_per_100_spaces`,
   `violations_per_100_occupied_spaces`, `violations_per_km_of_parking_edge`,
   `share_of_violations_near_full_occupancy`, разбивка по статусам, осадки, активные перекрытия.
   Сводка по району (`summary`) добавляет нормировку «в сутки», корреляцию Пирсона
   «загрузка ~ нарушения» по часам и медиану времени от начала эпизода высокой загрузки
   (≥ 90 %) до нарушения.
4. Отклонённые сообщения по умолчанию не считаются нарушениями (`--include-rejected`).

Длина кромки: для линий — их длина, для полигонов — половина периметра.

## Хранилище

- `data/raw/<источник>/` — неизменяемые снимки ответов API и импортированных файлов (одинаковое содержимое не дублируется).
- DuckDB: `parking_segments` (SCD2: `valid_from`/`valid_to`, представление `current_segments`), `parking_changes`,
  `occupancy_snapshots` (`source_type` = official_api | official_file | manual_import), `violation_events`,
  `road_events`, `weather_hourly`, витрина `violation_matches`, `violation_occupancy`, `hourly_area_metrics`,
  `hourly_area_type_metrics`.

## Известные ограничения

- Имена полей набора № 623 разбираются по списку вариантов (`FIELD_CANDIDATES` в `parkan/sources/mosdata.py`).
  Против живого API они пока не проверены: при первом `fetch-623` посмотрите на нераспознанные поля в столбце `extra`.
- Центроид сегмента — среднее вершин. Для поиска ближайшей парковки этого достаточно, для точной геометрии нужен PostGIS.
- Сеточный индекс рассчитан на широту Москвы.
