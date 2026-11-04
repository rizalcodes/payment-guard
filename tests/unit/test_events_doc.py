"""``docs/events.md`` tidak boleh menyimpang dari enum ``Event``.

Dokumen turunan yang tidak diperiksa akan basi, dan dokumen basi tentang
``audit_log`` lebih buruk daripada tidak ada dokumen: ia dipakai untuk membaca
bukti.

Yang diperiksa di sini hanya yang bisa diperiksa dengan jujur — daftar nama
kejadian dan actor yang sah untuk masing-masing. Prosa di dokumen itu tidak
diuji, dan tidak berpura-pura diuji.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from payment_guard.events import EVENT_ACTORS, Actor, Event, known_events

DOC = Path(__file__).resolve().parents[2] / "docs" / "events.md"

#: Baris tabel kejadian: ``| `nama` | `actor` | ... |``.
ROW = re.compile(r"^\|\s*`(?P<event>[a-z_]+)`\s*\|(?P<actors>[^|]*)\|")


def documented_rows() -> dict[str, set[str]]:
    """Nama kejadian -> himpunan actor, dari tabel di ``docs/events.md``.

    Baris yang kolom actor-nya berbunyi "sama seperti di atas" tetap punya
    kolom actor sendiri, jadi tidak ada baris yang perlu ditebak.
    """
    found: dict[str, set[str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match is None:
            continue
        event = match.group("event")
        actors = set(re.findall(r"`([a-z]+)`", match.group("actors")))
        if not actors:
            continue
        found[event] = actors
    return found


def test_the_document_exists() -> None:
    """Penjaga: tanpa berkasnya, tes di bawah lulus tanpa memeriksa apa pun."""
    assert DOC.is_file()


def test_rows_were_actually_parsed() -> None:
    """Penjaga kedua: kalau pola tabelnya berubah, jangan lulus diam-diam."""
    assert len(documented_rows()) == len(list(Event))


def test_every_event_is_documented() -> None:
    assert set(documented_rows()) == known_events()


def test_no_event_is_documented_that_does_not_exist() -> None:
    """Arah sebaliknya: nama di dokumen yang tidak ada di enum."""
    tidak_dikenal = set(documented_rows()) - known_events()
    assert tidak_dikenal == set(), tidak_dikenal


@pytest.mark.parametrize("event", sorted(known_events()))
def test_documented_actors_match_the_enum(event: str) -> None:
    rows = documented_rows()
    expected = {actor.value for actor in EVENT_ACTORS[Event(event)]}
    assert rows[event] == expected, f"{event}: dokumen {rows[event]}, kode {expected}"


def test_the_document_names_every_actor_it_uses() -> None:
    """Tidak ada actor karangan di dokumen."""
    sah = {actor.value for actor in Actor}
    for event, actors in documented_rows().items():
        assert actors <= sah, f"{event}: actor tidak dikenal {actors - sah}"


def test_the_rejection_reasons_are_listed() -> None:
    """Daftar tertutup ``check`` ikut didokumentasikan, dan lengkap."""
    from payment_guard.executor import REJECTION_REASONS

    teks = DOC.read_text(encoding="utf-8")
    for reason in REJECTION_REASONS:
        assert f"`{reason}`" in teks, reason


def test_the_failure_causes_are_listed() -> None:
    from payment_guard.executor import FAILURE_CAUSES

    teks = DOC.read_text(encoding="utf-8")
    for cause in FAILURE_CAUSES:
        assert cause in teks, cause
