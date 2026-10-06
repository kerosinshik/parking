import json
import re
from datetime import date, datetime

import duckdb
import pytest

from parkan.track import metrics
from parkan.track.dashboard import render_dashboard
from parkan.track.store import current_sql, event_files
from parkan.track.streams import STREAMS
from parkan.track.sync import IncompleteSnapshot, sync_dataset

LIC = STREAMS["licenses"].datasets[0]
LOTS = STREAMS["parking_lots"].datasets[0]
EM, EW = STREAMS["works"].datasets


class FakeClient:
    """Отдаёт заданные записи постранично, как /features у apidata.mos.ru."""

    page_size = 2

    def __init__(self, data: dict[int, list[dict]], count_override: dict | None = None):
        self.data, self.count_override = data, count_override or {}

    def count(self, ds):
        return self.count_override.get(ds, len(self.data[ds]))

    def features_page(self, ds, skip, top=None):
        rows = self.data[ds][skip:skip + self.page_size]
        return [{"type": "Feature", "geometry": r.get("_geom"),
                 "properties": {"attributes": {k: v for k, v in r.items() if k != "_geom"}}} for r in rows]


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def lic(i, inn, addr, state, begin, state_d=None, job="РПО", district="район А", name=None):
    return {"global_id": 1000 + i, "ID": i, "ObjectName": name or f"Точка {i}", "SubjectName": f"ООО {inn}",
            "INN": inn, "District": district, "Address": addr, "JobType": job, "CurrentLicenseState": state,
            "LicenseBegin": begin, "InstallDateOfCurrentLicenseState": state_d or begin, "DateOfDecision": begin,
            "_geom": {"type": "Point", "coordinates": [37.6, 55.75]}}


def test_sync_records_added_changed_removed(con, tmp_path):
    data = {586: [lic(1, "1", "ул. А, 1", "действующая", "01.09.2026"),
                  lic(2, "2", "ул. Б, 2", "действующая", "01.09.2026"),
                  lic(3, "3", "ул. В, 3", "действующая", "01.09.2026")]}
    r = sync_dataset(con, FakeClient(data), LIC, tmp_path, observed_at=datetime(2026, 10, 1, 9))
    assert (r.added, r.changed, r.removed, r.fetched) == (3, 0, 0, 3)

    r = sync_dataset(con, FakeClient(data), LIC, tmp_path, observed_at=datetime(2026, 10, 2, 9))
    assert (r.added, r.changed, r.removed, r.file) == (0, 0, 0, None)      # без изменений файл не пишется

    data[586][0]["CurrentLicenseState"] = "прекращена"
    data[586][0]["InstallDateOfCurrentLicenseState"] = "02.10.2026"
    del data[586][2]
    data[586].append(lic(4, "4", "ул. Г, 4", "действующая", "03.10.2026"))
    r = sync_dataset(con, FakeClient(data), LIC, tmp_path, observed_at=datetime(2026, 10, 3, 9))
    assert (r.added, r.changed, r.removed) == (1, 1, 1)
    assert r.changed_fields == {"CurrentLicenseState": 1, "InstallDateOfCurrentLicenseState": 1}
    assert len(event_files(tmp_path, 586)) == 2

    now = dict(con.execute(f"SELECT record_id, json_extract_string(payload, '$.CurrentLicenseState') "
                           f"FROM ({current_sql(tmp_path, 586)})").fetchall())
    assert now == {"1001": "прекращена", "1002": "действующая", "1004": "действующая"}
    # состояние на прошлую дату восстанавливается из журнала
    before = {r[0] for r in con.execute(f"SELECT record_id FROM ({current_sql(tmp_path, 586, datetime(2026, 10, 2, 12))})").fetchall()}
    assert before == {"1001", "1002", "1003"}


def test_sync_refuses_incomplete_snapshot(con, tmp_path):
    data = {586: [lic(i, str(i), f"ул. {i}", "действующая", "01.09.2026") for i in range(4)]}
    sync_dataset(con, FakeClient(data), LIC, tmp_path, observed_at=datetime(2026, 10, 1))
    with pytest.raises(IncompleteSnapshot):
        sync_dataset(con, FakeClient(data, count_override={586: 100}), LIC, tmp_path, observed_at=datetime(2026, 10, 2))
    assert len(event_files(tmp_path, 586)) == 1            # ложные удаления не записаны


def test_license_points_openings_and_closures(con, tmp_path):
    data = {586: [
        # точка 1: старая лицензия прекращена и продлена новой — это не закрытие и не новое открытие
        lic(1, "1", "ул. А, 1", "прекращена", "01.01.2025", "20.09.2026"),
        lic(2, "1", "ул.  А, 1", "действующая", "20.09.2026"),
        # точка 2: открылась недавно
        lic(3, "2", "ул. Б, 2", "действующая", "28.09.2026", job="РПА", district="район Б"),
        # точка 3: закрылась недавно
        lic(4, "3", "ул. В, 3", "прекращена", "01.03.2024", "01.10.2026"),
    ]}
    sync_dataset(con, FakeClient(data), LIC, tmp_path, observed_at=datetime(2026, 10, 3))
    m = metrics.licenses(con, tmp_path, date(2026, 10, 6))
    assert m["points"] == 3 and m["records"] == 4
    assert m["tiles"]["РПО"] == {"active": 1, "opened30": 0, "closed30": 1}
    assert m["tiles"]["РПА"] == {"active": 1, "opened30": 1, "closed30": 0}
    kinds = sorted((r["kind"], r["name"]) for r in m["recent"])
    assert kinds == [("закрытие", "Точка 4"), ("открытие", "Точка 3")]
    assert sum(m["series"]["РПА"]["opened"]) == 1 and sum(m["series"]["РПО"]["closed"]) == 1


def test_parking_lots_metrics(con, tmp_path):
    def lot(i, kind, stage, price, space, start, end):
        return {"global_id": 5000 + i, "ID": i, "ObjectType": kind, "Stage": stage, "StartPrice": price,
                "Space": space, "District": "район А", "Address": f"ул. {i}", "StartReceptionDate": start,
                "EndReceptionDate": end, "TradesDate": end, "Photo": ["x"],
                "_geom": {"type": "Point", "coordinates": [37.6, 55.75]}}
    data = {1461: [lot(1, "машино-место", "опубликовано", 2_000_000, 16, "01.10.2026", "20.10.2026"),
                   lot(2, "машино-место", "опубликовано", 1_000_000, 10, "15.09.2026", "15.10.2026"),
                   lot(3, "машино-место", "снято с публикации", 900_000, 9, "01.06.2026", "01.07.2026"),
                   lot(4, "квартира", "опубликовано", 9_000_000, 50, "01.10.2026", "20.10.2026")]}
    sync_dataset(con, FakeClient(data), LOTS, tmp_path, observed_at=datetime(2026, 10, 3))
    raw = con.execute(f"SELECT payload FROM ({current_sql(tmp_path, 1461)}) LIMIT 1").fetchone()[0]
    assert "Photo" not in json.loads(raw)                 # лишние поля не попадают в журнал
    m = metrics.parking_lots(con, tmp_path, date(2026, 10, 6))
    t = m["tiles"]
    assert (t["on_sale"], t["new30"], t["median_price"], t["total"]) == (2, 2, 1_500_000, 3)
    assert t["median_m2"] == pytest.approx((125_000 + 100_000) / 2)
    assert m["districts"][0]["on_sale"] == 2 and m["upcoming"][0]["address"] == "ул. 2" and m["upcoming"][0]["lots"] == 1

    data[1461][0]["Stage"] = "снято с публикации"
    sync_dataset(con, FakeClient(data), LOTS, tmp_path, observed_at=datetime(2026, 10, 4))
    assert metrics.parking_lots(con, tmp_path, date(2026, 10, 6))["stage_changes"] == 1


def test_works_metrics_and_dashboard(con, tmp_path):
    em = [{"global_id": 1, "EmCallRegNum": "A1", "EmCallDate": "05.10.2026", "WorkStartDate": "05.10.2026",
           "WorkEndDate": "12.10.2026", "EngineeringNetObj": "Тепловая сеть", "LeadOfWork": "ПАО МОЭК",
           "District": "район А", "SignOfEmergency": "С отключением абонентов", "IsCrashSignOfEmergency": "Да",
           "WorkPlaceDescription": "двор"},
          {"global_id": 2, "EmCallRegNum": "A2", "EmCallDate": "20.09.2026", "WorkStartDate": "20.09.2026",
           "WorkEndDate": "25.09.2026", "EngineeringNetObj": "Объекты электросетевого хозяйства", "LeadOfWork": "Россети",
           "District": "район Б", "SignOfEmergency": "Без отключения абонентов", "IsCrashSignOfEmergency": "Нет",
           "WorkPlaceDescription": "тротуар"}]
    ew = [{"global_id": 9, "RegistrationNumberNotifications": "У1", "Date": "01.10.2026", "WorkStartDate": "02.10.2026",
           "WorkEndDate": "30.10.2026", "WorkType": "['1. Земляные работы', '2. Установка временных ограждений']",
           "District": "район А"}]
    client = FakeClient({62461: em, 62501: ew})
    for ds in (EM, EW):
        sync_dataset(con, client, ds, tmp_path, observed_at=datetime(2026, 10, 6))
    w = metrics.works(con, tmp_path, date(2026, 10, 6))
    assert w["em_tiles"] == {"active": 1, "active_outage": 1, "new7": 1, "new30": 2}
    assert w["ew_tiles"] == {"active_earth": 1, "active_all": 1, "new30": 1}
    assert sum(w["em_series"]["Теплосети"]) == 1 and sum(w["em_series"]["Электросети"]) == 1
    assert w["executors"][0]["outage_share"] in (0.0, 1.0) and w["parking"] is None

    out = render_dashboard(con, tmp_path, tmp_path / "d.html", today=date(2026, 10, 6))
    data = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text(encoding="utf-8")).group(1))
    assert data["licenses"] is None and data["works"]["em_tiles"]["active"] == 1
    assert {l["dataset"] for l in data["log"]} == {62461, 62501}
    frag = render_dashboard(con, tmp_path, tmp_path / "f.html", today=date(2026, 10, 6), fragment=True).read_text(encoding="utf-8")
    assert frag.startswith("<title>") and "<body>" not in frag


def test_digest_helpers():
    from parkan.track.digest import _robust_z, org, plural
    assert [plural(n, "авария", "аварии", "аварий") for n in (1, 2, 5, 11, 21, 104)] == \
        ["авария", "аварии", "аварий", "аварий", "авария", "аварии"]
    assert org('Общество  с ограниченной ответственностью "Лента"') == 'ООО "Лента"'
    assert org("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «МОЭК»") == "ПАО «МОЭК»"
    assert _robust_z(95, [23] * 12) > 3 and _robust_z(24, [20, 22, 23, 25, 24, 21, 23, 22, 26, 24]) < 3


def test_digest_findings(con, tmp_path):
    from parkan.track import digest

    def lot(i, price, addr, start="01.10.2026"):
        return {"global_id": 7000 + i, "ID": i, "ObjectType": "машино-место", "Stage": "опубликовано",
                "StartPrice": price, "Space": 13.3, "District": "район А", "Address": addr,
                "StartReceptionDate": start, "EndReceptionDate": "30.10.2026", "TradesDate": "05.11.2026"}
    lots = [lot(i, 2_000_000 + i * 1000, f"ул. {i}") for i in range(8)]
    sync_dataset(con, FakeClient({1461: lots}), LOTS, tmp_path, observed_at=datetime(2026, 10, 5, 7))
    # следующий день: новый дешёвый лот и снижение цены у существующего
    lots.append(lot(100, 1_200_000, "ул. Дешёвая, 1", start="05.10.2026"))
    lots[0]["StartPrice"] = 1_500_000
    # сеть открыла 4 точки за неделю, раньше точек не было
    lic_rows = [lic(i, "77", f"ул. Сеть, {i}", "действующая", "02.10.2026", name="Отдохни") for i in range(4)]
    em = [{"global_id": 300 + i, "EmCallRegNum": f"E{i}", "EmCallDate": "05.10.2026", "WorkStartDate": "05.10.2026",
           "WorkEndDate": "12.10.2026", "EngineeringNetObj": "Тепловая сеть", "LeadOfWork": "ПАО МОЭК",
           "District": "район Б", "SignOfEmergency": "С отключением абонентов" if i == 0 else "Без отключения абонентов",
           "IsCrashSignOfEmergency": "Да", "WorkPlaceDescription": f"дом {i}"} for i in range(5)]
    client = FakeClient({1461: lots, 586: lic_rows, 62461: em})
    for ds in (LOTS, LIC, EM):
        sync_dataset(con, client, ds, tmp_path, observed_at=datetime(2026, 10, 6, 7))
    items = digest.findings(con, tmp_path, date(2026, 10, 6), limit=30)
    kinds = {(f.stream, f.kind) for f in items}
    assert ("lots", "дешевле района") in kinds and ("lots", "снижение цены") in kinds
    cheap = next(f for f in items if f.kind == "дешевле района")
    assert "ул. Дешёвая, 1" in cheap.detail and "40 %" in cheap.title
    drop = next(f for f in items if f.kind == "снижение цены")
    assert "2,00 млн ₽ → 1,50 млн ₽" in drop.detail
    assert any(f.kind == "сеть" and "открыто 4 точки" in f.title for f in items)
    assert any(f.kind == "район" and "район Б: 5 новых аварий" in f.title for f in items)
    assert sum(f.kind == "отключение" for f in items) == 1
    assert items == sorted(items, key=lambda f: -f.score)
    md = digest.to_markdown(items, date(2026, 10, 6), "https://example.test")
    assert md.startswith("Находки дня, 06.10.2026") and md.endswith("Дашборд: https://example.test")


def test_digest_flags_mass_removal(con, tmp_path):
    from parkan.track import digest
    rows = [lic(i, str(i), f"ул. {i}", "действующая", "01.01.2025") for i in range(50)]
    sync_dataset(con, FakeClient({586: rows}), LIC, tmp_path, observed_at=datetime(2026, 10, 5))
    sync_dataset(con, FakeClient({586: rows[:45]}), LIC, tmp_path, observed_at=datetime(2026, 10, 6), min_ratio=0.5)
    items = digest.findings(con, tmp_path, date(2026, 10, 6))
    assert any(f.stream == "data" and "исчезло 5 записей" in f.title for f in items)
