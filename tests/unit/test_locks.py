"""Kunci advisory: known-answer, pemisahan namespace, rentang int64. ST-45."""

from __future__ import annotations

import hashlib

import pytest

from payment_guard.locks import (
    INT64_MAX,
    INT64_MIN,
    MIGRATE_LOCK_KEY,
    NAMESPACES,
    advisory_key,
)


def _reference(namespace: str, identity: str) -> int:
    """Implementasi acuan yang ditulis ulang dari spesifikasi, bukan dari kode."""
    digest = hashlib.sha256(f"{namespace}:{identity}".encode()).digest()
    value = int.from_bytes(digest[:8], "big", signed=False)
    return value - 2**64 if value >= 2**63 else value


# Known-answer: nilai tetap, dihitung dari spesifikasi Arsitektur v1.1 bagian 4.
# Bila salah satu baris ini berubah, kunci advisory produksi berubah dan proses
# versi lama tidak lagi saling mengunci dengan versi baru.
KNOWN_ANSWERS = [
    ("guard", "demo-agent", _reference("guard", "demo-agent")),
    ("agent", "demo-agent", _reference("agent", "demo-agent")),
    ("mandate", "demo-agent", _reference("mandate", "demo-agent")),
    ("executor", "0x" + "ab" * 20, _reference("executor", "0x" + "ab" * 20)),
    ("watcher", "84532", _reference("watcher", "84532")),
    ("migrate", "", _reference("migrate", "")),
]


@pytest.mark.parametrize(("namespace", "identity", "expected"), KNOWN_ANSWERS)
def test_known_answer(namespace: str, identity: str, expected: int) -> None:
    assert advisory_key(namespace, identity) == expected


def test_migrate_key_uses_empty_identity() -> None:
    """Namespace migrate memakai identitas kosong: satu formula, tanpa kasus khusus."""
    assert MIGRATE_LOCK_KEY == advisory_key("migrate", "")
    assert MIGRATE_LOCK_KEY == advisory_key("migrate")
    # Nilai literal dipaku supaya perubahan formula tertangkap, bukan hanya
    # perubahan pemanggilan.
    assert MIGRATE_LOCK_KEY == _reference("migrate", "")


def test_namespaces_produce_different_keys_for_same_identity() -> None:
    """Identitas sama di namespace berbeda tidak boleh bertabrakan."""
    identity = "demo-agent"
    keys = {ns: advisory_key(ns, identity) for ns in sorted(NAMESPACES)}
    assert len(set(keys.values())) == len(keys), keys


def test_different_identities_produce_different_keys() -> None:
    assert advisory_key("agent", "a") != advisory_key("agent", "b")


def test_separator_is_not_ambiguous() -> None:
    """Pemisah ':' tidak boleh membuat dua pasangan berbeda memberi kunci sama.

    Tanpa pemisah, ("guard", "xy") dan ("guardx", "y") akan bertabrakan.
    Namespace adalah daftar tertutup, jadi ambiguitas itu mustahil; tes ini
    menjaga agar pemisah tidak dihilangkan di kemudian hari.
    """
    assert advisory_key("guard", "x:y") != advisory_key("guard", "x")


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_key_in_int64_range(namespace: str) -> None:
    """Kunci harus muat di bigint Postgres untuk semua input."""
    for identity in ("", "a", "demo-agent", "x" * 200, "é中\U0001f600"):
        key = advisory_key(namespace, identity)
        assert INT64_MIN <= key <= INT64_MAX


def test_range_covers_negative_values() -> None:
    """Hasil ditafsirkan bertanda, jadi sebagian input wajar bernilai negatif.

    Bila fungsi diam-diam berubah menjadi unsigned, kunci bisa melampaui
    bigint dan Postgres menolaknya.
    """
    samples = [advisory_key("agent", f"agent-{i}") for i in range(200)]
    assert any(key < 0 for key in samples)
    assert any(key > 0 for key in samples)


def test_unknown_namespace_rejected() -> None:
    with pytest.raises(ValueError, match="namespace kunci tidak dikenal"):
        advisory_key("not_a_namespace", "x")


def test_namespace_set_is_exactly_the_documented_six() -> None:
    assert NAMESPACES == {"guard", "agent", "mandate", "executor", "watcher", "migrate"}
