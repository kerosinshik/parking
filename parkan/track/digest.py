"""Находки дня: аномалии и примечательные события в трёх потоках.

Находка — отклонение от нормы, а не просто новая запись. Каждой присваивается вес,
в сводку попадают самые сильные. Окно — «вчера» (последний полный день) и всё, что
журнал увидел после предыдущей синхронизации.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from . import metrics
from .store import event_files, events_source, tracking_start

STREAM_TITLES = {"lots": "Машино-места", "licenses": "Общепит и алкоритейл", "works": "Помехи по адресу",
                 "data": "Качество данных"}


@dataclass
class Finding:
    stream: str
    kind: str
    title: str
    detail: str
    score: float        # 0..1, чем больше — тем примечательнее
    key: str = ""       # постоянный ключ: одна и та же находка в разные дни даёт один ключ


def previous_sync(state_dir, dataset_ids) -> datetime | None:
    """Время предпоследней синхронизации (по именам файлов журнала)."""
    times = set()
    for ds in dataset_ids:
        for f in event_files(state_dir, ds):
            times.add(datetime.strptime(f.stem.removeprefix("events-"), "%Y%m%dT%H%M%S"))
    times = sorted(times)
    return times[-2] if len(times) >= 2 else None


def _robust_z(value: float, history: list[float]) -> float:
    """Насколько значение выше обычного: (x − медиана) / (1,4826·MAD), с нижней границей разброса."""
    if len(history) < 10:
        return 0.0
    med = statistics.median(history)
    mad = statistics.median(abs(h - med) for h in history) * 1.4826
    return (value - med) / max(mad, 1.0, 0.25 * med)


def _addr(s: str | None) -> str:
    s = s or ""
    for p in ("Российская Федерация, ", "город Москва, "):
        if s.startswith(p):
            s = s[len(p):]
    if s.startswith("внутригородская территория"):
        s = s.split(", ", 1)[-1]
    return s


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(4, "авария", "аварии", "аварий") -> "аварии"."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


ORG = (("непубличное акционерное общество", "АО"), ("публичное акционерное общество", "ПАО"),
       ("акционерное общество", "АО"), ("общество с ограниченной ответственностью", "ООО"),
       ("индивидуальный предприниматель", "ИП"))


def org(name: str | None) -> str:
    s = " ".join((name or "").split())
    low = s.lower()
    for long, short in ORG:
        i = low.find(long)
        if i >= 0:
            s, low = s[:i] + short + s[i + len(long):], low[:i] + short.lower() + low[i + len(long):]
    return s


def _num(v: float, nd: int = 1) -> str:
    return f"{v:.{nd}f}".rstrip("0").rstrip(".").replace(".", ",") if nd else f"{v:.0f}"


def _mln(v) -> str:
    return f"{v / 1e6:.2f}".replace(".", ",") + " млн ₽"


# ---------------------------------------------------------------- машино-места

def lot_findings(con, state_dir, today: date, since: datetime | None) -> list[Finding]:
    if metrics.parking_lots(con, state_dir, today) is None:
        return []
    out = []
    on_sale = "stage = 'опубликовано' AND (end_d IS NULL OR end_d >= ?)"
    # «новые»: появились в журнале после прошлой синхронизации; при первой — начало приёма заявок вчера-сегодня
    new_cond = "first_seen > ?" if since else "start_d >= ?"
    new_arg = since if since else today - timedelta(days=1)
    rows = con.execute(f"""
        WITH sale AS (SELECT *, price / nullif(space, 0) AS m2 FROM lots WHERE {on_sale} AND price > 0),
        dist AS (SELECT district, median(price) AS med, count(*) AS n FROM sale GROUP BY 1),
        city AS (SELECT quantile_cont(m2, 0.05) AS p05 FROM sale)
        SELECT s.address, s.district, count(*) AS lots, min(s.price) AS price, median(s.space) AS space,
               min(s.m2) AS m2, any_value(d.med) AS med, any_value(d.n) AS n, any_value(c.p05) AS p05,
               strftime(min(s.end_d), '%d.%m.%Y') AS until_d, arg_min(s.lot_id, s.price) AS lot_id,
               arg_min(s.site, s.price) AS site
        FROM sale s JOIN dist d USING (district) CROSS JOIN city c
        WHERE s.{new_cond}
        GROUP BY s.address, s.district
    """, [today, new_arg]).fetchall()
    cheapest = None
    for address, district, lots, price, space, m2, med, n, p05, until_d, lot_id, site in rows:
        link = metrics.lot_url(site, lot_id)
        until_d = f"{until_d}; лот: {link}" if link else until_d
        if cheapest is None or price < cheapest[3]:
            cheapest = (address, district, lots, price, space, until_d)
        ratio = price / med if med else 1
        if n >= 5 and ratio <= 0.75:
            out.append(Finding("lots", "дешевле района",
                               f"Машино-место на {round((1 - ratio) * 100)} % дешевле медианы района",
                               f"{_addr(address)} ({district}): {_mln(price)} при медиане {_mln(med)}"
                               f"{f'; лотов по адресу: {lots}' if lots > 1 else ''}; заявки до {until_d}",
                               min(1.0, 0.5 + (1 - ratio)), key=f"lots:cheap:{address}:{until_d.split(';')[0]}"))
        elif p05 and m2 is not None and m2 <= p05:
            out.append(Finding("lots", "дёшево за м²", "Новый лот среди 5 % самых дешёвых по цене за м²",
                               f"{_addr(address)} ({district}): {round(m2):,} ₽/м², ".replace(",", " ")
                               + f"{_mln(price)}; заявки до {until_d}", 0.55, key=f"lots:cheap:{address}:{until_d.split(';')[0]}"))
    if cheapest:
        a, d, lots, price, space, until_d = cheapest
        out.append(Finding("lots", "самый дешёвый новый", "Самый дешёвый новый лот",
                           f"{_addr(a)} ({d}): {_mln(price)}"
                           f"{f', {space:g} м²'.replace('.', ',') if space else ''}; заявки до {until_d}", 0.4,
                           key=f"lots:cheapest:{a}:{until_d.split(';')[0]}"))
    # снижение стартовой цены у той же записи (видно только по журналу)
    src = events_source(state_dir, 1461)
    if src and since:
        for rid, new_p, old_p, addr, district, lot_id, site in con.execute(f"""
            WITH ev AS (
                SELECT record_id, observed_at, try_cast(json_extract_string(payload, '$.StartPrice') AS DOUBLE) AS price,
                       json_extract_string(payload, '$.Address') AS address, json_extract_string(payload, '$.District') AS district,
                       json_extract_string(payload, '$.ObjectType') AS kind,
                       json_extract_string(payload, '$.ID') AS lot_id,
                       json_extract_string(payload, '$.WebSite[0].WebSite') AS site,
                       lag(try_cast(json_extract_string(payload, '$.StartPrice') AS DOUBLE))
                           OVER (PARTITION BY record_id ORDER BY observed_at) AS prev
                FROM {src} WHERE event <> 'removed'
            )
            SELECT record_id, price, prev, address, district, lot_id, site FROM ev
            WHERE observed_at > ? AND kind = 'машино-место' AND prev > 0 AND price < prev * 0.9
        """, [since]).fetchall():
            drop = 1 - new_p / old_p
            out.append(Finding("lots", "снижение цены", f"Стартовая цена снижена на {round(drop * 100)} %",
                               f"{_addr(addr)} ({district}): {_mln(old_p)} → {_mln(new_p)}"
                               + (f"; лот: {metrics.lot_url(site, lot_id)}" if metrics.lot_url(site, lot_id) else ""),
                               min(1.0, 0.6 + drop),
                               key=f"lots:drop:{rid}:{new_p:.0f}"))
    return out


# ---------------------------------------------------------------- общепит и алкоритейл

def license_findings(con, state_dir, today: date) -> list[Finding]:
    if metrics.licenses(con, state_dir, today) is None:
        return []
    out = []
    day = today - timedelta(days=1)
    hist_from = day - timedelta(days=90)
    for kind, col in (("открытий", "opened"), ("закрытий", "closed")):
        counts = dict(con.execute(f"SELECT {col}, count(*) FROM lic_points WHERE {col} BETWEEN ? AND ? GROUP BY 1",
                                  [hist_from, day]).fetchall())
        # сравниваем с теми же днями недели: в выходные реестр почти не пополняется
        hist = [counts.get(hist_from + timedelta(days=i), 0) for i in range(90)
                if (hist_from + timedelta(days=i)).weekday() == day.weekday()]
        value = counts.get(day, 0)
        z = _robust_z(value, hist)
        if z >= 3 and value >= 5:
            out.append(Finding("licenses", "всплеск", f"Необычно много {kind} точек за {day:%d.%m}",
                               f"{value} при обычных {_num(statistics.median(hist))} в такой день недели",
                               min(1.0, 0.4 + z / 10), key=f"licenses:spike:{col}:{day}"))
    # сети: несколько точек за неделю, заметно больше своего обычного темпа
    for subject, opened7, closed7, opened90, closed90, active in con.execute("""
        SELECT subject,
               count(*) FILTER (WHERE opened > ?), count(*) FILTER (WHERE closed > ?),
               count(*) FILTER (WHERE opened > ?), count(*) FILTER (WHERE closed > ?),
               count(*) FILTER (WHERE active)
        FROM lic_points GROUP BY 1
        HAVING count(*) FILTER (WHERE opened > ?) >= 3 OR count(*) FILTER (WHERE closed > ?) >= 3
    """, [today - timedelta(days=7)] * 2 + [today - timedelta(days=90)] * 2 + [today - timedelta(days=7)] * 2).fetchall():
        for n7, n90, verb in ((opened7, opened90, "открыто"), (closed7, closed90, "закрыто")):
            weekly = n90 / 13
            if n7 >= 3 and n7 >= 2 * max(weekly, 1):
                out.append(Finding("licenses", "сеть", f"{org(subject)}: {verb} {n7} {plural(n7, 'точка', 'точки', 'точек')} за неделю",
                                   f"обычно ~{_num(weekly)} в неделю; сейчас действует: {active}",
                                   min(1.0, 0.45 + n7 / 40),
                                   key=f"licenses:chain:{verb}:{subject}:{today.isocalendar()[0]}-{today.isocalendar()[1]}"))
    return out


# ---------------------------------------------------------------- помехи по адресу

def works_findings(con, state_dir, today: date) -> list[Finding]:
    w = metrics.works(con, state_dir, today)
    if not w or "em_tiles" not in w:
        return []
    out = []
    day = today - timedelta(days=1)
    # До начала учёта закрытые вызовы в наборе не видны, поэтому норма — только по дням,
    # целиком прошедшим после первой синхронизации, и по дням того же типа (будни / выходные)
    since = tracking_start(state_dir, 62461)
    start = max(day - timedelta(days=84), since.date() + timedelta(days=1)) if since else day
    counts = dict(con.execute("SELECT reg_d, count(*) FROM em WHERE reg_d BETWEEN ? AND ? GROUP BY 1",
                              [start, day]).fetchall())
    workday = day.weekday() < 5
    hist = [counts.get(d, 0) for d in (start + timedelta(days=i) for i in range((day - start).days))
            if (d.weekday() < 5) == workday]
    value = counts.get(day, 0)
    z = _robust_z(value, hist)
    if z >= 3 and value >= 10:
        out.append(Finding("works", "всплеск аварий", f"Всплеск аварийных работ за {day:%d.%m}",
                           f"{value} {plural(value, 'новый вызов', 'новых вызова', 'новых вызовов')} при обычных "
                           f"{_num(statistics.median(hist))} {'в будний день' if workday else 'в выходной'}",
                           min(1.0, 0.5 + z / 10), key=f"works:spike:{day}"))
    for district, n, nets in con.execute("""
        SELECT district, count(*), string_agg(DISTINCT net, '; ') FROM em
        WHERE reg_d = ? AND district IS NOT NULL GROUP BY 1 HAVING count(*) >= 4 ORDER BY 2 DESC
    """, [day]).fetchall():
        out.append(Finding("works", "район", f"{district}: {n} {plural(n, 'новая авария', 'новые аварии', 'новых аварий')} за день",
                           nets or "", min(1.0, 0.35 + n / 20), key=f"works:district:{district}:{day}"))
    for district, net, lead, place, start_d, end_d in con.execute("""
        SELECT district, net, lead, place, strftime(start_d, '%d.%m'), strftime(end_d, '%d.%m') FROM em
        WHERE outage AND reg_d >= ? ORDER BY reg_d DESC LIMIT 10
    """, [day]).fetchall():
        out.append(Finding("works", "отключение", f"Авария с отключением абонентов: {district}",
                           f"{net}; {_addr(place)}; {start_d}–{end_d}", 0.5,
                           key=f"works:outage:{district}:{place}:{start_d}"))
    return out


# ---------------------------------------------------------------- качество данных

def data_findings(con, state_dir, dataset_ids, since: datetime | None) -> list[Finding]:
    if not since:
        return []
    out = []
    for ds in dataset_ids:
        src = events_source(state_dir, ds)
        if not src:
            continue
        total, removed, changed = con.execute(f"""
            WITH last AS (SELECT * FROM {src} QUALIFY row_number() OVER (PARTITION BY record_id ORDER BY observed_at DESC) = 1)
            SELECT count(*) FILTER (WHERE event <> 'removed' OR observed_at > ?),
                   count(*) FILTER (WHERE event = 'removed' AND observed_at > ?),
                   count(*) FILTER (WHERE event = 'changed' AND observed_at > ?)
            FROM last
        """, [since] * 3).fetchone()
        if total and removed / total > 0.01:
            out.append(Finding("data", "удаления", f"Набор № {ds}: исчезло {removed} записей ({removed / total:.1%})",
                               "проверьте, событие это или сбой источника", 0.7, key=f"data:removed:{ds}:{since:%Y%m%d%H%M}"))
        if total and changed / total > 0.10:
            out.append(Finding("data", "изменения", f"Набор № {ds}: изменено {changed} записей ({changed / total:.1%})",
                               "массовое обновление у источника", 0.5, key=f"data:changed:{ds}:{since:%Y%m%d%H%M}"))
    return out


def findings(con, state_dir, today: date, limit: int = 12) -> list[Finding]:
    from .streams import STREAMS
    ids = [ds.id for s in STREAMS.values() for ds in s.datasets]
    since = previous_sync(state_dir, ids)
    found = (lot_findings(con, state_dir, today, since) + license_findings(con, state_dir, today)
             + works_findings(con, state_dir, today) + data_findings(con, state_dir, ids, since))
    found.sort(key=lambda f: -f.score)
    # не больше 4 находок одного вида, чтобы сводку не забил один поток
    per_kind, out = {}, []
    for f in found:
        k = (f.stream, f.kind)
        if per_kind.get(k, 0) < 4:
            per_kind[k] = per_kind.get(k, 0) + 1
            out.append(f)
    return out[:limit]


def to_markdown(items: list[Finding], today: date, dashboard_url: str | None = None) -> str:
    lines = [f"Находки дня, {today:%d.%m.%Y}"]
    if not items:
        lines.append("Ничего необычного: все потоки в пределах нормы.")
    for f in items:
        lines.append(f"• [{STREAM_TITLES[f.stream]}] {f.title} — {f.detail}")
    if dashboard_url:
        lines.append(f"Дашборд: {dashboard_url}")
    return "\n".join(lines)


def to_json(items: list[Finding]) -> list[dict]:
    return [asdict(f) for f in items]


def dumps(items) -> str:
    return json.dumps(to_json(items), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- реестр отправленных находок

def load_registry(path) -> dict:
    """{ключ: дата первой отправки}. Хранится рядом с журналом, чтобы не дублировать находки."""
    from pathlib import Path
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def unsent(items: list[Finding], registry: dict) -> list[Finding]:
    return [f for f in items if f.key not in registry]


def mark_sent(path, keys, today: date, keep_days: int = 60) -> dict:
    """Добавляет ключи в реестр и забывает записи старше keep_days."""
    from pathlib import Path
    reg = load_registry(path)
    for k in keys:
        reg.setdefault(k, today.isoformat())
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    reg = {k: v for k, v in reg.items() if v >= cutoff}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return reg
