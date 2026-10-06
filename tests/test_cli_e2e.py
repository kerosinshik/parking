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
    areas = {a["summary"]["area"]: a for a in data["areas"]}
    assert set(areas) == {"район Арбат", "район Хамовники"}
    arbat, ham = areas["район Арбат"]["summary"], areas["район Хамовники"]["summary"]
    # в демо заложено: нарушений больше при загрузке > 75 %, а в Арбате загрузка выше
    assert arbat["corr_occupancy_violations"] > 0.5
    assert arbat["violations_per_100_spaces_per_day"] > ham["violations_per_100_spaces_per_day"]
    assert arbat["near_full_share"] > ham["near_full_share"]
    assert data["coverage"]["with_occupancy"] / data["coverage"]["total"] > 0.9
    assert len(areas["район Арбат"]["heat"]) == 7 and len(areas["район Арбат"]["segments"]) == 20

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
