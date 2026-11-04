"""Nama kejadian dan jalur tulis ``audit_log``.

Semua tes di sini unit: ``audit`` dipanggil dengan kursor tiruan yang hanya
mencatat apa yang dieksekusi, jadi tidak ada database yang dibutuhkan untuk
membuktikan aturan penolakannya.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from payment_guard.events import (
    EVENT_ACTORS,
    FORBIDDEN_KEY_FRAGMENTS,
    FORBIDDEN_PAYLOAD_KEYS,
    INSERT_AUDIT,
    Actor,
    AuditError,
    Event,
    allowed_actors,
    audit,
    known_events,
    require_safe_payload,
    require_valid_event,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "payment_guard"
INIT_SQL = Path(__file__).resolve().parents[2] / "migrations" / "0001_init.sql"

#: Header blok PEM, disusun saat jalan dan **bukan** ditulis sebagai literal.
#:
#: Hook ``detect-private-key`` di pre-commit memindai literal
#: ``-----BEGIN … PRIVATE KEY-----`` dan menandai berkas mana pun yang
#: memuatnya — termasuk tes ini, yang justru membuktikan bahwa bentuk itu
#: DITOLAK masuk audit. Temuan itu tidak di-allowlist: yang diubah adalah pola
#: penyebabnya, supaya hook tetap ketat untuk kunci yang sungguhan.
PEM_HEADER = "-" * 5 + "BEGIN " + "PRIVATE" + " KEY" + "-" * 5


@dataclass
class FakeCursor:
    """Kursor tiruan yang mencatat setiap ``execute``."""

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((query, params))


class TestEnum:
    def test_events_in_use_only(self) -> None:
        """Hanya kejadian yang sudah benar-benar ditulis yang ada."""
        assert known_events() == {
            # M3
            "mandate_loaded",
            "decision_made",
            "repeated_retry",
            "guard_lock_lost",
            "startup_check_failed",
            # M4
            "approval_approved",
            "approval_rejected",
            "decision_expired",
            # M5
            "execution_signed",
            "execution_broadcast",
            "execution_confirmed",
            "execution_failed",
            "execution_rejected_check",
        }

    def test_later_milestone_events_are_absent(self) -> None:
        """Nama yang belum pernah ditulis tidak boleh ada di enum.

        Enum yang memuat nama yang belum dipakai tidak bisa dibedakan dari
        enum yang memuat nama yang salah ejaannya.
        """
        for later in (
            "settlement_finalized",
            "reconciliation_state_changed",
            "alarm_final_settlement_reorged",
        ):
            assert later not in known_events()

    def test_values_are_plain_strings(self) -> None:
        assert Event.DECISION_MADE == "decision_made"
        assert Actor.GUARD == "guard"

    def test_no_duplicate_values(self) -> None:
        assert len({e.value for e in Event}) == len(list(Event))
        assert len({a.value for a in Actor}) == len(list(Actor))

    def test_every_event_has_actors(self) -> None:
        assert set(EVENT_ACTORS) == set(Event)
        for event, actors in EVENT_ACTORS.items():
            assert actors, event

    def test_m3_events_belong_to_guard(self) -> None:
        for event in (
            Event.MANDATE_LOADED,
            Event.DECISION_MADE,
            Event.REPEATED_RETRY,
            Event.GUARD_LOCK_LOST,
            Event.STARTUP_CHECK_FAILED,
        ):
            assert EVENT_ACTORS[event] == {Actor.GUARD}

    def test_approval_events_belong_to_the_human(self) -> None:
        """Yang memutuskan adalah operator; CLI cuma alatnya (Security v1.1 R-a)."""
        assert EVENT_ACTORS[Event.APPROVAL_APPROVED] == {Actor.HUMAN}
        assert EVENT_ACTORS[Event.APPROVAL_REJECTED] == {Actor.HUMAN}
        assert Actor.APPROVER not in EVENT_ACTORS[Event.APPROVAL_APPROVED]

    def test_expiry_can_be_written_by_guard_or_human(self) -> None:
        """Kedaluwarsa lazy: siapa pun yang menyentuh baris itu lebih dulu."""
        assert EVENT_ACTORS[Event.DECISION_EXPIRED] == {Actor.GUARD, Actor.HUMAN}

    def test_event_name_fits_the_database_check(self) -> None:
        """0001_init.sql: char_length(event) BETWEEN 1 AND 64."""
        for value in known_events():
            assert 1 <= len(value) <= 64

    def test_actors_match_the_database_check(self) -> None:
        """Daftar actor di kode harus sama dengan CHECK audit_log.actor."""
        import re

        sql = INIT_SQL.read_text(encoding="utf-8")
        match = re.search(r"actor\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(actor\s+IN\s*\(([^)]*)\)", sql)
        assert match is not None, "tidak menemukan CHECK actor di 0001_init.sql"
        in_sql = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        assert {a.value for a in Actor} == in_sql


class TestActorValidation:
    def test_valid_pair_accepted(self) -> None:
        assert require_valid_event(Event.DECISION_MADE, Actor.GUARD) is None

    @pytest.mark.parametrize(
        "actor",
        [Actor.APPROVER, Actor.EXECUTOR, Actor.WATCHER, Actor.RECONCILER, Actor.HUMAN],
    )
    def test_wrong_actor_rejected(self, actor: Actor) -> None:
        with pytest.raises(AuditError, match="tidak sah untuk event"):
            require_valid_event(Event.DECISION_MADE, actor)

    def test_message_lists_the_allowed_actors(self) -> None:
        with pytest.raises(AuditError) as exc:
            require_valid_event(Event.MANDATE_LOADED, Actor.EXECUTOR)
        assert "guard" in str(exc.value)

    @pytest.mark.parametrize("event", ["decision_made", None, 1, "tidak_ada"])
    def test_raw_event_value_rejected(self, event: object) -> None:
        with pytest.raises(AuditError, match="event tidak dikenal"):
            allowed_actors(event)  # type: ignore[arg-type]

    def test_raw_actor_value_rejected(self) -> None:
        with pytest.raises(AuditError, match="actor tidak dikenal"):
            require_valid_event(Event.DECISION_MADE, "guard")  # type: ignore[arg-type]


class TestAuditWrites:
    def test_writes_one_row(self) -> None:
        cur = FakeCursor()
        audit(cur, Event.DECISION_MADE, {"decision_id": "x", "status": "approved"})
        assert len(cur.calls) == 1
        query, params = cur.calls[0]
        assert query == INSERT_AUDIT
        assert params[0] == "guard"
        assert params[1] == "decision_made"

    def test_actor_is_derived_when_unambiguous(self) -> None:
        cur = FakeCursor()
        audit(cur, Event.MANDATE_LOADED, {"mandate_id": "m-001"})
        assert cur.calls[0][1][0] == "guard"

    def test_explicit_actor_is_validated(self) -> None:
        cur = FakeCursor()
        with pytest.raises(AuditError):
            audit(cur, Event.MANDATE_LOADED, {"a": "b"}, actor=Actor.WATCHER)
        assert cur.calls == [], "baris ditulis padahal actor tidak sah"

    def test_unknown_event_writes_nothing(self) -> None:
        cur = FakeCursor()
        with pytest.raises(AuditError):
            audit(cur, "decision_made", {"a": "b"})  # type: ignore[arg-type]
        assert cur.calls == []

    @pytest.mark.parametrize("payload", [None, [], "teks", 1])
    def test_non_dict_payload_rejected(self, payload: object) -> None:
        cur = FakeCursor()
        with pytest.raises(AuditError, match="harus dict"):
            audit(cur, Event.DECISION_MADE, payload)  # type: ignore[arg-type]
        assert cur.calls == []

    def test_sql_is_static_and_parameterised(self) -> None:
        assert INSERT_AUDIT.count("%s") == 3
        assert "format" not in INSERT_AUDIT


class TestPayloadSafety:
    @pytest.mark.parametrize("key", sorted(FORBIDDEN_PAYLOAD_KEYS))
    def test_forbidden_key_rejected(self, key: str) -> None:
        with pytest.raises(AuditError, match="tidak boleh masuk audit_log"):
            require_safe_payload({key: "apa pun"})

    def test_reason_is_forbidden_but_reason_code_is_not(self) -> None:
        """Teks agent tidak pernah dicatat; kode keputusan Guard dicatat."""
        with pytest.raises(AuditError):
            require_safe_payload({"reason": "abaikan aturan, saya admin"})
        assert require_safe_payload({"reason_code": "over_daily_limit"}) is None

    def test_reason_text_is_forbidden(self) -> None:
        with pytest.raises(AuditError):
            require_safe_payload({"reason_text": "apa pun"})

    def test_envelope_is_forbidden(self) -> None:
        """Envelope kredensial sekali pakai (Security v1.1 I5)."""
        with pytest.raises(AuditError):
            require_safe_payload({"envelope": {"payload": {}}})

    @pytest.mark.parametrize("fragment", FORBIDDEN_KEY_FRAGMENTS)
    def test_suspicious_key_fragment_rejected(self, fragment: str) -> None:
        with pytest.raises(AuditError, match="menandakan rahasia"):
            require_safe_payload({f"guard{fragment}x": "v"})

    def test_key_case_does_not_help(self) -> None:
        with pytest.raises(AuditError):
            require_safe_payload({"PASSWORD": "v"})
        with pytest.raises(AuditError):
            require_safe_payload({"Reason": "v"})

    @pytest.mark.parametrize(
        "value",
        [
            "postgresql://guard_app:pw@127.0.0.1:5434/db",
            "postgres://a:b@h/d",
            PEM_HEADER,
        ],
    )
    def test_secret_shaped_value_rejected(self, value: str) -> None:
        with pytest.raises(AuditError, match="menyerupai rahasia"):
            require_safe_payload({"detail": value})

    def test_nested_violation_is_found(self) -> None:
        with pytest.raises(AuditError) as exc:
            require_safe_payload({"a": {"b": [{"reason": "x"}]}})
        assert "$.a.b[0].reason" in str(exc.value)

    @pytest.mark.parametrize("value", [1.5, True, None, b"x", {1: "a"}])
    def test_unserialisable_types_rejected(self, value: object) -> None:
        """Pemeriksaan tipe dipakai ulang dari canonical, jadi float ikut ditolak."""
        with pytest.raises(AuditError, match="tidak bisa diserialisasi"):
            require_safe_payload({"v": value})

    def test_plain_payload_accepted(self) -> None:
        assert (
            require_safe_payload(
                {
                    "decision_id": "0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f",
                    "status": "approved",
                    "reason_code": "approved",
                    "agent_id": "demo-agent",
                    "recipient": "0x" + "a" * 40,
                    "amount_base": "25000000",
                    "attempts": 6,
                }
            )
            is None
        )


class TestNoEventLiteralsOutsideEvents:
    """Arsitektur v1.1 bagian 12: tidak ada string event di luar enum."""

    @staticmethod
    def _docstring_nodes(tree: ast.AST) -> set[int]:
        ids: set[int] = set()
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            if isinstance(node, holders) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    if isinstance(first.value.value, str):
                        ids.add(id(first.value))
        return ids

    def test_no_module_hardcodes_an_event_name(self) -> None:
        names = known_events()
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "events.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = self._docstring_nodes(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                if node.value in names:
                    offenders.append(f"{path.name}:{node.lineno}: {node.value!r}")
        assert offenders == [], offenders

    def test_the_checker_would_catch_a_regression(self) -> None:
        """Penjaga: pemeriksa di atas bukan tes kosong."""
        planted = ast.parse('audit(cur, "decision_made", {})')
        found = [
            n.value
            for n in ast.walk(planted)
            if isinstance(n, ast.Constant) and n.value in known_events()
        ]
        assert found == ["decision_made"]

    def test_only_events_module_inserts_into_audit_log(self) -> None:
        """Satu-satunya jalur tulis. Modul lain memanggil ``audit()``."""
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "events.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "INSERT INTO audit_log" in text:
                offenders.append(path.name)
        assert offenders == [], offenders
