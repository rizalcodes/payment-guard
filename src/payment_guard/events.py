"""Nama kejadian dan satu-satunya jalur tulis ke ``audit_log``.

Arsitektur v1.1 bagian 12: "Hanya ``events.py`` menulis ``audit_log``. Tidak
ada string event di luar enum." Ada tes AST yang menegakkan keduanya.

Alasannya bukan kerapian. ``audit_log`` adalah bukti (Security v1.1 A4), dan
bukti yang nama kejadiannya diketik ulang di sepuluh tempat akan punya sebelas
ejaan. Satu enum berarti ``docs/events.md`` di M9 bisa dibuat dari kode, dan
CHECK ``event`` di database bisa ditambahkan tanpa menemukan kejutan.

Hanya kejadian yang sudah dipakai yang ada di sini: enum yang memuat nama yang
belum pernah ditulis tidak bisa dibedakan dari enum yang memuat nama yang
salah. ``execution_*`` masuk di M5 karena di situlah Executor mulai
menulisnya, dan ``settlement_*``, ``alarm_*``, serta
``reconciliation_state_changed`` masuk di M6 karena di situlah Watcher dan
Reconciler mulai menulisnya. Semuanya punya pemanggil dan tesnya; ada tes yang
menolak kejadian tanpa pemanggil di ``src/``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

import psycopg
from psycopg.types.json import Jsonb

from payment_guard.canonical import CanonicalError, validate

#: SQL statik; nilai selalu lewat parameter.
INSERT_AUDIT: Final = "INSERT INTO audit_log (actor, event, payload) VALUES (%s, %s, %s)"


class Actor(StrEnum):
    """Pelaku kejadian. Sama dengan CHECK ``audit_log.actor``."""

    GUARD = "guard"
    APPROVER = "approver"
    EXECUTOR = "executor"
    WATCHER = "watcher"
    RECONCILER = "reconciler"
    HUMAN = "human"
    MIGRATOR = "migrator"


class Event(StrEnum):
    """Kejadian yang sudah dipakai. Daftar tertutup."""

    #: Guard start memuat mandate dan memperbarui ``active_mandates``.
    MANDATE_LOADED = "mandate_loaded"
    #: Satu keputusan ``check_payment`` tercatat.
    DECISION_MADE = "decision_made"
    #: Lebih dari lima ``retry_blocked`` dalam satu jendela cooldown
    #: (PRD v2 bagian 6.4). Sinyal agent yang mencoba memetakan batas.
    REPEATED_RETRY = "repeated_retry"
    #: Heartbeat gagal: koneksi lock hilang, Guard berhenti menjawab.
    GUARD_LOCK_LOST = "guard_lock_lost"
    #: Pemeriksaan saat start gagal, mis. Guard kedua untuk agent yang sama.
    STARTUP_CHECK_FAILED = "startup_check_failed"

    # --- M4: persetujuan manusia (Arsitektur v1.1 bagian 7) ---
    #: Operator menyetujui satu ``needs_human``; envelope ditandatangani.
    APPROVAL_APPROVED = "approval_approved"
    #: Operator menolak, atau cek ulang per_tx/daily gagal saat approve.
    APPROVAL_REJECTED = "approval_rejected"
    #: ``needs_human`` melewati ``expires_at`` dan menjadi ``expired``.
    DECISION_EXPIRED = "decision_expired"

    # --- M5: eksekusi on-chain (PRD v2 bagian 9, PRD v2.1 bagian 9) ---
    #: Transaksi sudah ditandatangani dan ditulis write-ahead, belum disiarkan.
    #: Inilah baris yang membuat recovery mungkin: ia ada sebelum ada apa pun
    #: di jaringan, jadi crash sesudahnya tidak kehilangan jejak.
    EXECUTION_SIGNED = "execution_signed"
    #: ``eth_sendRawTransaction`` diterima node.
    EXECUTION_BROADCAST = "execution_broadcast"
    #: Receipt diterima dengan ``status = 1``.
    EXECUTION_CONFIRMED = "execution_confirmed"
    #: Receipt revert, atau nonce-nya dipakai transaksi lain. **Bukan** timeout.
    EXECUTION_FAILED = "execution_failed"
    #: Salah satu cek Executor gagal; tidak ada transaksi yang dibangun.
    #:
    #: Jenis cek yang gagal ada di ``payload["check"]``, dari daftar tertutup
    #: :data:`payment_guard.executor.REJECTION_REASONS`, **bukan** di nama
    #: kejadian. Nama yang mengkodekan jenis akan berarti satu nama baru setiap
    #: kali daftar cek bertambah, dan enum ini berhenti bisa dipakai sebagai
    #: daftar. Keputusan operator, 4 November 2026.
    EXECUTION_REJECTED = "execution_rejected"

    # --- M6: Watcher (PRD v2 bagian 10, Arsitektur v1.1 bagian 10) ---
    #
    # Lima nama di bawah diambil apa adanya dari tabel state machine di PRD v2
    # bagian 10 dan dari diagram sequence Arsitektur bagian 10. Tidak ada nama
    # baru yang dikarang di M6.
    #
    # Yang sengaja TIDAK ada: kejadian untuk INSERT settlement `pending`.
    # Arsitektur bagian 10 menuliskan langkah itu sebagai "INSERT settlement
    # pending ON CONFLICT DO NOTHING, naikkan last_scanned, COMMIT" tanpa
    # audit, dan memang begitu seharusnya: setiap transfer dari wallet
    # Executor akan menghasilkan satu baris, jadi mencatatnya lagi di
    # `audit_log` hanya menduplikasi `settlements` dengan tabel yang tidak bisa
    # di-UPDATE. Yang perlu jejak adalah **perubahan** keadaan.
    #: Settlement melewati kedalaman konfirmasi dan hash bloknya masih kanonik.
    SETTLEMENT_FINALIZED = "settlement_finalized"
    #: Hash blok berubah tapi transaksinya masih ada: baris yang sama dipindah
    #: ke blok baru. Kunci uniknya tidak berubah, jadi tidak ada baris ganda.
    SETTLEMENT_MOVED = "settlement_moved"
    #: Hash blok berubah **dan** transaksinya hilang. Baris tidak dihapus:
    #: yang pernah terlihat di chain tetap tercatat pernah terlihat.
    SETTLEMENT_REORGED = "settlement_reorged"
    #: Transaksi yang sudah ``reorged`` muncul lagi di blok lain.
    SETTLEMENT_RESTORED = "settlement_restored"
    #: Baris yang sudah ``final`` ternyata ter-reorg di dalam jendela mundur K.
    #:
    #: Inilah satu-satunya kejadian M6 yang berawalan ``alarm_``, dan awalan
    #: itu bukan hiasan: ``final`` adalah janji yang kami buat ke rekonsiliasi,
    #: dan transisi ini adalah janji itu dibatalkan. Rekonsiliasinya menjadi
    #: ``invalidated`` (PRD v2 bagian 11).
    ALARM_FINAL_SETTLEMENT_REORGED = "alarm_final_settlement_reorged"

    # --- M6: Reconciler (PRD v2 bagian 11, Arsitektur v1.1 bagian 11) ---
    #: State rekonsiliasi satu subjek berubah.
    #:
    #: Satu nama untuk semua state, bukan satu nama per state. Arsitektur
    #: bagian 11 menuliskannya begitu ("Setiap perubahan state ditulis ke
    #: ``audit_log`` (``reconciliation_state_changed``)"), dan alasannya sama
    #: dengan ``execution_rejected`` di M5: state-nya ada di payload, dari
    #: daftar tertutup :data:`payment_guard.reconciler.RECONCILIATION_STATES`.
    #: Nama yang mengkodekan state berarti satu nama baru setiap kali daftar
    #: state bertambah, dan enum ini berhenti bisa dipakai sebagai daftar.
    RECONCILIATION_STATE_CHANGED = "reconciliation_state_changed"


#: Actor yang sah untuk tiap kejadian.
#:
#: Kejadian M3 semuanya ditulis Guard. Kejadian M4 ditulis ``human``: yang
#: menekan tombol adalah operator, dan Approvals CLI hanyalah alatnya. Memakai
#: ``approver`` di sini akan menyamarkan itu menjadi tindakan proses, padahal
#: nilai bukti terbesarnya justru "ada manusia yang memutuskan" (Security v1.1
#: R-a: sangkalan operator).
#:
#: ``decision_expired`` menerima ``guard`` **dan** ``human``. Kedaluwarsa itu
#: lazy (Arsitektur v1.1 bagian 7): siapa pun yang pertama menyentuh baris itu
#: yang menuliskannya, dan dua role itulah yang punya hak UPDATE kolom status
#: di ``0002_roles.sql``. Di M4 hanya jalur ``human`` yang dipakai.
#:
#: Kejadian M5 semuanya ``executor``, dan hanya itu. Berbeda dari persetujuan,
#: di sini memang prosesnya yang bertindak: tidak ada manusia yang menekan
#: apa pun di antara tanda tangan dan broadcast, dan justru itu yang membuat
#: write-ahead perlu. ``execution_failed`` juga tidak pernah ``human``: ia
#: menyatakan apa yang dilihat Executor di chain, bukan penilaian siapa pun.
EVENT_ACTORS: Final[dict[Event, frozenset[Actor]]] = {
    Event.MANDATE_LOADED: frozenset({Actor.GUARD}),
    Event.DECISION_MADE: frozenset({Actor.GUARD}),
    Event.REPEATED_RETRY: frozenset({Actor.GUARD}),
    Event.GUARD_LOCK_LOST: frozenset({Actor.GUARD}),
    Event.STARTUP_CHECK_FAILED: frozenset({Actor.GUARD}),
    Event.APPROVAL_APPROVED: frozenset({Actor.HUMAN}),
    Event.APPROVAL_REJECTED: frozenset({Actor.HUMAN}),
    Event.DECISION_EXPIRED: frozenset({Actor.GUARD, Actor.HUMAN}),
    Event.EXECUTION_SIGNED: frozenset({Actor.EXECUTOR}),
    Event.EXECUTION_BROADCAST: frozenset({Actor.EXECUTOR}),
    Event.EXECUTION_CONFIRMED: frozenset({Actor.EXECUTOR}),
    Event.EXECUTION_FAILED: frozenset({Actor.EXECUTOR}),
    Event.EXECUTION_REJECTED: frozenset({Actor.EXECUTOR}),
    # Kejadian M6 Watcher semuanya `watcher`, dan hanya itu. Tidak ada manusia
    # di jalur ini sama sekali: Watcher melaporkan apa yang dilihatnya di
    # chain. Reconciler tidak boleh menulisnya meski ia membaca hasilnya —
    # satu penulis per fakta (Arsitektur v1.1 prinsip 2).
    Event.SETTLEMENT_FINALIZED: frozenset({Actor.WATCHER}),
    Event.SETTLEMENT_MOVED: frozenset({Actor.WATCHER}),
    Event.SETTLEMENT_REORGED: frozenset({Actor.WATCHER}),
    Event.SETTLEMENT_RESTORED: frozenset({Actor.WATCHER}),
    Event.ALARM_FINAL_SETTLEMENT_REORGED: frozenset({Actor.WATCHER}),
    Event.RECONCILIATION_STATE_CHANGED: frozenset({Actor.RECONCILER}),
}

#: Kunci payload yang ditolak, apa pun isinya.
#:
#: ``reason`` dan ``reason_text`` ada di sini karena field ``reason`` dari agent
#: tidak boleh mengalir ke mana pun selain kolom ``decisions.reason_text``
#: (PRD v2.1 bagian 3 nomor 10, Security v1.1 T-8, I3). ``envelope`` dan
#: ``signature`` ada di sini karena envelope adalah kredensial sekali pakai
#: (Security v1.1 I5), bukan karena ia rahasia abadi.
#:
#: ``reason_code`` **boleh**: ia enum tertutup milik Guard, bukan teks agent.
FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reason",
        "reason_text",
        "envelope",
        "signature",
        "signed_decision",
        "password",
        "secret",
        "private_key",
        "signing_key",
        "token",
        "api_key",
        "db_url",
    }
)

#: Potongan nama kunci yang menandakan rahasia, diperiksa sebagai substring.
FORBIDDEN_KEY_FRAGMENTS: Final[tuple[str, ...]] = ("password", "secret", "private", "_url")

#: Pola nilai yang tidak boleh masuk audit: URL koneksi dan blok kunci PEM.
FORBIDDEN_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"postgres(?:ql)?://", re.IGNORECASE),
    re.compile(r"-----BEGIN"),
)


class AuditError(ValueError):
    """Kejadian, actor, atau payload tidak sah. Tidak ada baris yang ditulis."""


def allowed_actors(event: Event) -> frozenset[Actor]:
    """Actor yang sah untuk ``event``."""
    if not isinstance(event, Event):
        raise AuditError(f"event tidak dikenal: {event!r}")
    return EVENT_ACTORS[event]


def require_valid_event(event: Event, actor: Actor) -> None:
    """Pastikan ``event`` ada di enum dan ``actor`` sah untuk kejadian itu.

    Raises:
        AuditError: bila salah satu tidak sah.
    """
    allowed = allowed_actors(event)
    if not isinstance(actor, Actor):
        raise AuditError(f"actor tidak dikenal: {actor!r}")
    if actor not in allowed:
        raise AuditError(
            f"actor {actor.value!r} tidak sah untuk event {event.value!r}; "
            f"yang sah: {', '.join(sorted(a.value for a in allowed))}"
        )


def require_safe_payload(payload: object, path: str = "$") -> None:
    """Pastikan payload tidak memuat rahasia dan tipenya bisa diserialisasi.

    Pemeriksaan tipe dipakai ulang dari :mod:`payment_guard.canonical`, jadi
    larangan ``float`` dan ``bool`` berlaku di sini juga tanpa definisi kedua.

    Raises:
        AuditError: bila ada kunci terlarang, nilai berpola rahasia, atau tipe
            yang tidak bisa diserialisasi.
    """
    try:
        validate(payload, path)
    except CanonicalError as exc:
        raise AuditError(f"payload audit tidak bisa diserialisasi: {exc}") from None
    _scan(payload, path)


def _scan(value: object, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in FORBIDDEN_PAYLOAD_KEYS:
                raise AuditError(
                    f"{path}.{key}: kunci itu tidak boleh masuk audit_log. "
                    "Teks dari agent dan bahan kredensial tidak pernah dicatat."
                )
            for fragment in FORBIDDEN_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise AuditError(
                        f"{path}.{key}: nama kunci mengandung {fragment!r}, yang menandakan rahasia"
                    )
            _scan(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(value):
                raise AuditError(f"{path}: nilainya menyerupai rahasia (pola {pattern.pattern!r})")


def audit(
    cur: psycopg.Cursor,
    event: Event,
    payload: dict[str, object],
    *,
    actor: Actor | None = None,
) -> None:
    """Tulis satu baris ``audit_log``. Satu-satunya jalur tulis ke tabel itu.

    Dijalankan pada kursor pemanggil, jadi ia berada di transaksi yang sama
    dengan kejadian yang dicatatnya: keputusan dan catatannya commit bersama
    atau tidak sama sekali.

    Args:
        cur: kursor di dalam transaksi pemanggil.
        event: kejadian dari :class:`Event`.
        payload: hanya ``str``, ``int``, ``dict``, dan ``list``.
        actor: boleh dikosongkan bila kejadian itu hanya punya satu actor sah.

    Raises:
        AuditError: bila kejadian, actor, atau payload tidak sah. Tidak ada
            baris yang ditulis.
    """
    allowed = allowed_actors(event)
    if actor is None:
        if len(allowed) != 1:
            raise AuditError(f"event {event.value!r} punya beberapa actor sah; sebutkan actor-nya")
        actor = next(iter(allowed))
    require_valid_event(event, actor)

    if not isinstance(payload, dict):
        raise AuditError(f"payload audit harus dict, bukan {type(payload).__name__}")
    require_safe_payload(payload)

    cur.execute(INSERT_AUDIT, (actor.value, event.value, Jsonb(payload)))


def known_events() -> frozenset[str]:
    """Nilai string semua kejadian. Dipakai tes dan, nanti, ``docs/events.md``."""
    return frozenset(event.value for event in Event)
