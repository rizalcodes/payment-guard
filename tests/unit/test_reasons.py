"""Status, reason_code, dan pasangannya.

Tes utama di sini membaca ``migrations/0001_init.sql`` dan mem-parse daftar
CHECK-nya. Daftar itu tidak pernah disalin ke dalam tes: kalau disalin, salinan
ketiga akan menyimpang dan tesnya justru menyembunyikan masalah.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from payment_guard.reasons import (
    NON_TRIGGERING_DENIALS,
    STATUS_REASONS,
    ReasonCode,
    ReasonPairError,
    Status,
    all_valid_pairs,
    cooldown_triggering_pairs,
    require_valid_pair,
    triggers_cooldown,
)

INIT_SQL = Path(__file__).resolve().parents[2] / "migrations" / "0001_init.sql"


# --------------------------------------------------------------------------- #
# Parser kecil untuk CHECK di 0001_init.sql
# --------------------------------------------------------------------------- #


def sql_text() -> str:
    return INIT_SQL.read_text(encoding="utf-8")


def parse_in_list(column: str) -> set[str]:
    """Ambil daftar literal dari ``<column> ... CHECK (<column> IN ( ... ))``."""
    pattern = re.compile(
        rf"{column}\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*{column}\s+IN\s*\((?P<items>[^)]*)\)",
        re.IGNORECASE,
    )
    match = pattern.search(sql_text())
    assert match is not None, f"tidak menemukan CHECK untuk kolom {column} di {INIT_SQL.name}"
    return set(re.findall(r"'([a-z_]+)'", match.group("items")))


def parse_status_reason_pairs() -> dict[str, set[str]]:
    """Ambil pemetaan status -> reason_code dari CONSTRAINT status_reason_pairs."""
    text = sql_text()
    start = text.index("CONSTRAINT status_reason_pairs CHECK (")
    end = text.index("CONSTRAINT approved_is_complete", start)
    block = text[start:end]

    pairs: dict[str, set[str]] = {}
    # Bentuk jamak: status = 'x' AND reason_code IN ('a','b')
    for status, items in re.findall(
        r"status\s*=\s*'(\w+)'\s*AND\s+reason_code\s+IN\s*\(([^)]*)\)", block
    ):
        pairs.setdefault(status, set()).update(re.findall(r"'([a-z_]+)'", items))
    # Bentuk tunggal: status = 'x' AND reason_code = 'y'
    for status, code in re.findall(
        r"status\s*=\s*'(\w+)'\s*AND\s+reason_code\s*=\s*'(\w+)'", block
    ):
        pairs.setdefault(status, set()).add(code)

    assert pairs, "tidak menemukan pasangan apa pun di status_reason_pairs"
    return pairs


class TestTheParserItself:
    """Kalau parser rusak, tes perbandingan di bawah jadi hampa."""

    def test_finds_reason_codes(self) -> None:
        codes = parse_in_list("reason_code")
        assert len(codes) == 12
        assert "approved" in codes
        assert "human_wait_timeout" in codes

    def test_finds_statuses(self) -> None:
        statuses = parse_in_list("status")
        assert statuses == {
            "approved",
            "denied",
            "needs_human",
            "rejected_by_human",
            "expired",
        }

    def test_finds_all_five_status_groups_in_pairs(self) -> None:
        pairs = parse_status_reason_pairs()
        assert set(pairs) == parse_in_list("status")

    def test_within_limits_is_not_in_the_sql(self) -> None:
        """PRD 6.4 menulis `within_limits`; SQL tidak punya kode itu.

        Operator memutuskan SQL yang menang (2 Nov 2026): langkah 6 memakai
        reason_code `approved`. Tes ini menahan keputusan itu supaya tidak
        hilang diam-diam.
        """
        assert "within_limits" not in parse_in_list("reason_code")


# --------------------------------------------------------------------------- #
# Enum di kode vs daftar di SQL
# --------------------------------------------------------------------------- #


class TestEnumMatchesSql:
    def test_reason_codes_are_exactly_the_same_set(self) -> None:
        in_code = {code.value for code in ReasonCode}
        in_sql = parse_in_list("reason_code")
        assert in_code == in_sql, (
            f"hanya di kode: {sorted(in_code - in_sql)}; hanya di SQL: {sorted(in_sql - in_code)}"
        )

    def test_statuses_are_exactly_the_same_set(self) -> None:
        in_code = {status.value for status in Status}
        in_sql = parse_in_list("status")
        assert in_code == in_sql, (
            f"hanya di kode: {sorted(in_code - in_sql)}; hanya di SQL: {sorted(in_sql - in_code)}"
        )

    def test_pairs_are_exactly_the_same(self) -> None:
        """Dua arah: tiap pasangan di kode ada di SQL, dan sebaliknya."""
        in_code = {
            status.value: {code.value for code in codes} for status, codes in STATUS_REASONS.items()
        }
        assert in_code == parse_status_reason_pairs()

    def test_every_reason_code_belongs_to_exactly_one_status(self) -> None:
        """Tidak ada kode yang menggantung, dan tidak ada yang dipakai dua status."""
        seen: dict[ReasonCode, Status] = {}
        for status, codes in STATUS_REASONS.items():
            for code in codes:
                assert code not in seen, f"{code} dipakai {seen.get(code)} dan {status}"
                seen[code] = status
        assert set(seen) == set(ReasonCode)


class TestRequireValidPair:
    @pytest.mark.parametrize(("status", "reason_code"), sorted(all_valid_pairs()))
    def test_valid_pairs_accepted(self, status: Status, reason_code: ReasonCode) -> None:
        assert require_valid_pair(status, reason_code) is None

    @pytest.mark.parametrize(
        ("status", "reason_code"),
        [
            (Status.APPROVED, ReasonCode.RETRY_BLOCKED),
            (Status.DENIED, ReasonCode.APPROVED),
            (Status.DENIED, ReasonCode.HUMAN_APPROVAL_REQUIRED),
            (Status.NEEDS_HUMAN, ReasonCode.OVER_DAILY_LIMIT),
            (Status.EXPIRED, ReasonCode.MANDATE_CHANGED),
            (Status.REJECTED_BY_HUMAN, ReasonCode.APPROVED_BY_HUMAN),
        ],
    )
    def test_invalid_pairs_rejected(self, status: Status, reason_code: ReasonCode) -> None:
        with pytest.raises(ReasonPairError, match="pasangan tidak sah"):
            require_valid_pair(status, reason_code)

    def test_message_lists_what_is_allowed(self) -> None:
        with pytest.raises(ReasonPairError) as exc:
            require_valid_pair(Status.NEEDS_HUMAN, ReasonCode.APPROVED)
        assert "human_approval_required" in str(exc.value)

    @pytest.mark.parametrize("value", ["approved", None, 1])
    def test_raw_values_are_not_accepted_as_status(self, value: object) -> None:
        with pytest.raises(ReasonPairError, match="status tidak dikenal"):
            require_valid_pair(value, ReasonCode.APPROVED)  # type: ignore[arg-type]

    def test_raw_value_is_not_accepted_as_reason_code(self) -> None:
        with pytest.raises(ReasonPairError, match="reason_code tidak dikenal"):
            require_valid_pair(Status.APPROVED, "approved")  # type: ignore[arg-type]


class TestCooldownTriggers:
    """PRD 6.4 kalimat terakhir, dan ST-11."""

    @pytest.mark.parametrize(
        "reason_code",
        [
            ReasonCode.RECIPIENT_NOT_ALLOWED,
            ReasonCode.OVER_PER_TX_LIMIT,
            ReasonCode.OVER_DAILY_LIMIT,
            ReasonCode.RECHECK_OVER_PER_TX_LIMIT,
            ReasonCode.RECHECK_OVER_DAILY_LIMIT,
        ],
    )
    def test_denied_triggers(self, reason_code: ReasonCode) -> None:
        assert triggers_cooldown(Status.DENIED, reason_code) is True

    def test_rejected_by_human_triggers(self) -> None:
        assert triggers_cooldown(Status.REJECTED_BY_HUMAN, ReasonCode.HUMAN_REJECTED) is True

    @pytest.mark.parametrize(
        ("status", "reason_code"),
        [
            (Status.DENIED, ReasonCode.RETRY_BLOCKED),
            (Status.DENIED, ReasonCode.MANDATE_CHANGED),
            (Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT),
            (Status.APPROVED, ReasonCode.APPROVED),
            (Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN),
            (Status.NEEDS_HUMAN, ReasonCode.HUMAN_APPROVAL_REQUIRED),
        ],
    )
    def test_does_not_trigger(self, status: Status, reason_code: ReasonCode) -> None:
        assert triggers_cooldown(status, reason_code) is False

    def test_non_triggering_denials_are_exactly_two(self) -> None:
        assert NON_TRIGGERING_DENIALS == {
            ReasonCode.RETRY_BLOCKED,
            ReasonCode.MANDATE_CHANGED,
        }

    def test_triggering_set_is_derived_not_duplicated(self) -> None:
        """Himpunan pemicu diturunkan dari predikat, bukan daftar kedua."""
        triggering = cooldown_triggering_pairs()
        assert triggering == {pair for pair in all_valid_pairs() if triggers_cooldown(*pair)}
        assert len(triggering) == 6

    def test_every_valid_pair_has_a_definite_answer(self) -> None:
        """Tidak ada pasangan sah yang membuat predikat melempar."""
        for status, reason_code in all_valid_pairs():
            assert isinstance(triggers_cooldown(status, reason_code), bool)

    def test_invalid_pair_raises_rather_than_guessing(self) -> None:
        with pytest.raises(ReasonPairError):
            triggers_cooldown(Status.APPROVED, ReasonCode.OVER_DAILY_LIMIT)


class TestEnumShape:
    def test_values_are_plain_strings(self) -> None:
        """StrEnum supaya nilainya bisa langsung dikirim sebagai parameter SQL."""
        assert Status.APPROVED == "approved"
        assert ReasonCode.OVER_DAILY_LIMIT == "over_daily_limit"

    def test_no_duplicate_values(self) -> None:
        assert len({code.value for code in ReasonCode}) == len(list(ReasonCode))
        assert len({status.value for status in Status}) == len(list(Status))

    def test_every_status_has_at_least_one_reason(self) -> None:
        assert set(STATUS_REASONS) == set(Status)
        for status, codes in STATUS_REASONS.items():
            assert codes, status
