"""Отслеживаемые потоки и их наборы на портале открытых данных Москвы."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dataset:
    id: int
    title: str
    key: str                       # поле-идентификатор записи, если нет global_id
    drop: tuple[str, ...] = ()     # поля, не влияющие на учёт (фото и т. п.)


@dataclass(frozen=True)
class Stream:
    name: str
    title: str
    datasets: tuple[Dataset, ...] = field(default_factory=tuple)


STREAMS: dict[str, Stream] = {s.name: s for s in (
    Stream("licenses", "Общепит и алкоритейл", (
        Dataset(586, "Лицензии на розничную продажу алкоголя", key="ID"),
    )),
    Stream("parking_lots", "Машино-места на торгах", (
        Dataset(1461, "Торги объектами городского имущества", key="ID", drop=("Photo",)),
    )),
    Stream("works", "Помехи по адресу", (
        Dataset(62461, "Аварийно-восстановительные работы на сетях", key="EmCallRegNum"),
        Dataset(62501, "Земляные работы и временные ограждения", key="RegistrationNumberNotifications"),
    )),
)}


def resolve(names: str | None) -> list[Stream]:
    if not names:
        return list(STREAMS.values())
    out = []
    for n in names.split(","):
        n = n.strip()
        if n not in STREAMS:
            raise ValueError(f"неизвестный поток {n!r}; доступны: {', '.join(STREAMS)}")
        out.append(STREAMS[n])
    return out
