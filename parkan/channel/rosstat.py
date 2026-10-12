"""Средние цены на рынке жилья Москвы по данным Росстата — фон для журнала рынка (market.py).

Росстат раз в квартал публикует XLSX «Средние цены на первичном/вторичном рынке жилья по субъектам РФ»
(rosstat.gov.ru/statistics/price): рубли за м² общей площади на конец квартала — все квартиры и по качеству
(низкого, типовые, улучшенные, элитные). Районов там нет, только Москва целиком, поэтому эти цифры — общий фон
(«по Москве на вторичке»), а не ориентир для конкретной квартиры.

rosstat.gov.ru подписан сертификатом Минцифры (Russian Trusted Root CA), которого нет в обычных хранилищах.
Сертификаты (корневой и промежуточный 2024, с gu-st.ru) лежат в assets/ru-trusted-ca.pem и добавляются
только к запросам этого модуля; проверка TLS не отключается.
"""

from __future__ import annotations

import io
import re
import ssl
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

PAGE = "https://rosstat.gov.ru/statistics/price"
BASE = "https://rosstat.gov.ru"
CA = Path(__file__).parent / "assets" / "ru-trusted-ca.pem"
MOSCOW = "45000000000"
SEGMENTS = {"vtor": "вторичка", "perv": "новостройки"}
QUALITY = {"низкого качества": "низкого качества", "квартиры среднего качества (типовые)": "типовые",
           "улучшенного качества": "улучшенные", "элитные квартиры": "элитные"}
QUARTER_END = {"I": (3, 31), "II": (6, 30), "III": (9, 30), "IV": (12, 31)}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(CA))
    return ctx


def fetch(url: str, timeout: int = 60, tries: int = 4) -> bytes:
    """GET с сертификатами Минцифры; соединение с rosstat.gov.ru иногда рвётся — несколько попыток."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "parkan/0.1 (+https://t.me/poputny_metr)"})
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
                return resp.read()
        except OSError as e:
            last = e
            if i + 1 < tries:
                import time
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def latest_files(page_html: str) -> dict[str, str]:
    """Ссылки на самые свежие файлы средних цен: {"vtor": url, "perv": url}. На странице свежие идут первыми."""
    out = {}
    for href in re.findall(r'href="(/storage/mediabank/sred_cen_(perv|vtor)_[^"]+\.xlsx)"', page_html):
        out.setdefault(href[1], BASE + href[0])
    return out


# ---------------------------------------------------------------- XLSX без сторонних библиотек

def _rows(xlsx: bytes, sheet_index: int) -> list[list]:
    """Строки листа как списки значений по колонкам (пустые ячейки — None)."""
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    sheets = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                    key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
    root = ElementTree.fromstring(z.read(sheets[sheet_index]))
    out = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        vals = {}
        for c in row.findall("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            v = c.find("m:v", NS)
            if c.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            elif v is None:
                continue
            elif c.get("t") == "s":
                text = shared[int(v.text)]
            else:
                text = v.text
            vals[idx - 1] = text
        out.append([vals.get(i) for i in range(max(vals) + 1)] if vals else [])
    return out


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(xlsx: bytes, segment: str, year: int, url: str) -> list[dict]:
    """Наблюдения по Москве из листа «в целом по субъекту РФ»: по кварталам, все квартиры и по качеству."""
    rows = _rows(xlsx, 1)
    quarters = next(r for r in rows if r and any(isinstance(c, str) and c.strip().endswith("квартал") for c in r))
    kinds = next(r for r in rows if r and any(isinstance(c, str) and c.strip() in QUALITY for c in r))
    moscow = next(r for r in rows if r and str(r[0] or "").startswith(MOSCOW))
    # колонки квартала: «Все типы квартир» стоит в колонке квартала, качества — следом до следующего квартала
    starts = [(i, c.strip().split()[0]) for i, c in enumerate(quarters) if isinstance(c, str) and c.strip().endswith("квартал")]
    out = []
    for n, (col, q) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(moscow)
        if q not in QUARTER_END:
            continue
        m, d = QUARTER_END[q]
        for i in range(col, end):
            value = _num(moscow[i] if i < len(moscow) else None)
            if value is None:
                continue
            quality = "все" if i == col else QUALITY.get((kinds[i] or "").strip() if i < len(kinds) else "")
            if not quality:
                continue
            out.append({"observed": date(year, m, d).isoformat(), "level": "город", "area": "Москва",
                        "segment": segment, "rooms": "все", "quality": quality, "metric": "sale_m2",
                        "value": str(round(value)), "source": "Росстат", "url": url,
                        "note": f"{q} квартал {year}, на конец квартала"})
    return out


def collect() -> list[dict]:
    """Свежие средние цены по Москве (вторичка и новостройки) — строки для market.add."""
    page = fetch(PAGE).decode("utf-8", "replace")
    out = []
    for key, url in latest_files(page).items():
        year = int(re.search(r"(20\d\d)", url.rsplit("/", 1)[1]).group(1))
        out += parse(fetch(url), SEGMENTS[key], year, url)
    return out
