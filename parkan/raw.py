"""Raw-хранилище неизменяемых снимков источников.

Каждый полученный ответ API или импортированный файл сохраняется один раз
под именем с меткой времени и хэшем. Существующие файлы не перезаписываются,
поэтому любую нормализацию можно повторить с исходных данных.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .timeutil import now_msk


def default_raw_dir() -> Path:
    return Path(os.environ.get("PARKAN_RAW_DIR", "data/raw"))


def store_bytes(con, source: str, data: bytes, *, suffix: str, raw_dir: Path | None = None,
                meta: dict | None = None) -> tuple[Path, bool]:
    """Сохраняет снимок. Возвращает (путь, новый_ли). Повтор с тем же содержимым не пишется."""
    raw_dir = Path(raw_dir) if raw_dir is not None else default_raw_dir()
    sha = hashlib.sha256(data).hexdigest()
    found = con.execute(
        "SELECT path FROM raw_files WHERE source = ? AND sha256 = ?", [source, sha]
    ).fetchone()
    if found:
        return Path(found[0]), False
    fetched_at = now_msk()
    target_dir = raw_dir / source
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{fetched_at:%Y%m%dT%H%M%S}_{sha[:12]}{suffix}"
    with open(path, "xb") as f:  # "x" — никогда не перезаписываем
        f.write(data)
    os.chmod(path, 0o444)
    con.execute(
        "INSERT INTO raw_files VALUES (?, ?, ?, ?, ?)",
        [source, sha, str(path), fetched_at, json.dumps(meta or {}, ensure_ascii=False)],
    )
    return path, True


def store_file(con, source: str, file_path: str | Path, raw_dir: Path | None = None) -> tuple[Path, bool]:
    file_path = Path(file_path)
    return store_bytes(con, source, file_path.read_bytes(), suffix=file_path.suffix or ".bin",
                       raw_dir=raw_dir, meta={"original_name": file_path.name})
