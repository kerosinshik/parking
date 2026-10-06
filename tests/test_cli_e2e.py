import json
import re

from parkan import cli, db


def run(*args):
    return cli.main([str(a) for a in args])


def test_demo_pipeline_end_to_end(tmp_path, capsys):
    work = tmp_path / "demo"
    assert run("demo", "--workdir", work, "--days", 7) == 0
    out = capsys.readouterr().out
    assert "Отчёт:" in out

    html = (work / "report.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const DATA = (.*?);\n", html).group(1))
    an = data["analytics"]["areas"]
    assert set(an) == {"район Арбат", "район Хамовники"}
    arbat, ham = an["район Арбат"]["summary"], an["район Хамовники"]["summary"]
    # в демо заложено: нарушений больше при загрузке > 75 %, а в Арбате загрузка выше
    assert arbat["corr_occupancy_violations"] > 0.5
    assert arbat["violations_per_100_spaces_per_day"] > ham["violations_per_100_spaces_per_day"]
    assert arbat["near_full_share"] > ham["near_full_share"]
    cov = data["analytics"]["coverage"]
    assert cov["with_occupancy"] / cov["total"] > 0.9
    assert len(an["район Арбат"]["heat"]) == 7
    infra = data["infra"]
    assert infra["city"]["segments"] == 40 and len(infra["areas"]) == 2
    assert {a["price_median"] for a in infra["areas"]} == {380, 250}

    # повторный прогон того же конвейера на той же базе идемпотентен
    con = db.connect(work / "parkan.duckdb")
    before = con.execute("SELECT (SELECT count(*) FROM violation_events), (SELECT count(*) FROM occupancy_snapshots),"
                         " (SELECT count(*) FROM parking_segments)").fetchone()
    con.close()
    assert run("--db", work / "parkan.duckdb", "--raw-dir", work / "raw",
               "import-violations", work / "input/violations.csv") == 0
    assert run("--db", work / "parkan.duckdb", "load-623", work / "input/mos_623_snapshot.json") == 0
    con = db.connect(work / "parkan.duckdb")
    after = con.execute("SELECT (SELECT count(*) FROM violation_events), (SELECT count(*) FROM occupancy_snapshots),"
                        " (SELECT count(*) FROM parking_segments)").fetchone()
    con.close()
    assert before == after

    assert run("--db", work / "parkan.duckdb", "export", "--out", work / "export") == 0
    assert (work / "export/mart/hourly_area_metrics.parquet").exists()
    assert run("--db", work / "parkan.duckdb", "summary", "--json") == 0


def test_cli_reports_errors_without_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MOS_API_KEY", raising=False)
    assert run("--db", tmp_path / "x.duckdb", "fetch-623") == 1
    assert "MOS_API_KEY" in capsys.readouterr().err
    bad = tmp_path / "v.csv"
    bad.write_text("violation_id,source_system,violation_type,status,occurred_at,фио\n1,a,b,проверено,2026-09-07 12:00,Иванов\n",
                   encoding="utf-8")
    assert run("--db", tmp_path / "x.duckdb", "--raw-dir", tmp_path / "raw", "import-violations", bad) == 2
    assert "персональными данными" in capsys.readouterr().err


def test_report_with_directory_only(tmp_path):
    """Только справочник (как сейчас с реальными открытыми данными): дашборд строится без витрины."""
    from parkan import demo
    demo.generate(tmp_path / "in", days=1, segments_per_area=5)
    dbp = tmp_path / "p.duckdb"
    assert run("--db", dbp, "--raw-dir", tmp_path / "raw", "load-623", tmp_path / "in/mos_623_snapshot.json") == 0
    assert run("--db", dbp, "report", "--out", tmp_path / "r.html") == 0
    html = (tmp_path / "r.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const DATA = (.*?);\n", html).group(1))
    assert data["analytics"]["areas"] == {} and data["analytics"]["coverage"] is None
    assert data["infra"]["city"]["capacity"] == sum(a["capacity"] for a in data["infra"]["areas"]) > 0
    assert data["infra"]["last_changes"] == {"added": 10}
    seg = data["infra"]["segments"][0]
    assert seg["geom"]["type"] == "LineString" and seg["area"].startswith("район ")


def test_hour_price_parsing():
    from parkan.report import hour_price
    assert hour_price('[{"TimeRange": "08:00-21:00", "HourPrice": 380}, {"HourPrice": 200}]') == 380
    assert hour_price('{"Tariff": "от 100 до 300 руб./час"}') == 300
    assert hour_price("250") == 250
    assert hour_price('[{"TimeRange": "08:00-21:00"}]') is None
    assert hour_price(None) is None
