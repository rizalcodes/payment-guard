"""Aturan keputusan. ST-11, ST-12, batas inklusif, dan kemurnian fungsi.

Semua waktu di sini adalah detik UTC yang disuntikkan. Tidak ada tes yang
membaca jam sistem, karena :func:`evaluate` pun tidak boleh.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from payment_guard.mandate import Mandate, load_mandate
from payment_guard.reasons import ReasonCode, ReasonPairError, Status
from payment_guard.rules import (
    Decision,
    Denial,
    Request,
    RuleInputError,
    Snapshot,
    cooldown_ends_at,
    evaluate,
)

#: Dua alamat dalam allowlist, dan satu di luar.
ALLOWED_A = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
ALLOWED_B = "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"
OUTSIDER = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"

#: 1 USDC dalam base units, dipakai untuk membuat angka tes terbaca.
USDC = 1_000_000

#: Jam database palsu. Angka bulat supaya aritmetika cooldown mudah dibaca.
NOW = 1_762_000_000

COOLDOWN = 3600


def mandate(**overrides: Any) -> Mandate:
    data: dict[str, Any] = {
        "mandate_id": "demo-2026-11",
        "agent_id": "demo-agent",
        "chain": "base-sepolia",
        "allowed_recipients": [ALLOWED_A, ALLOWED_B],
        "max_per_tx": "25",
        "max_per_day": "100",
        "human_approval_above": "25",
        "retry_cooldown_seconds": COOLDOWN,
        "aggregation_window_seconds": 3600,
        "decision_ttl_seconds": 900,
        "human_wait_ttl_seconds": 900,
    }
    data.update(overrides)
    return load_mandate(data)


def request(amount_base: int = 1 * USDC, recipient: str = ALLOWED_A) -> Request:
    return Request(recipient=recipient, amount_base=amount_base, chain_id=84532)


def snapshot(
    committed_day: int = 0,
    committed_recipient: int = 0,
    denial: Denial | None = None,
) -> Snapshot:
    return Snapshot(
        committed_day=committed_day,
        committed_recipient=committed_recipient,
        latest_triggering_denial=denial,
    )


def denial_at(
    decided_at: int,
    status: Status = Status.DENIED,
    reason_code: ReasonCode = ReasonCode.OVER_PER_TX_LIMIT,
) -> Denial:
    return Denial(decided_at=decided_at, status=status, reason_code=reason_code)


# --------------------------------------------------------------------------- #
# Urutan evaluasi dan jalur bahagia
# --------------------------------------------------------------------------- #


class TestApprovedPath:
    def test_within_all_limits_is_approved(self) -> None:
        decision = evaluate(request(1 * USDC), mandate(), snapshot(), NOW)
        assert decision.status is Status.APPROVED
        assert decision.reason_code is ReasonCode.APPROVED
        assert decision.retry_after is None

    def test_step_six_uses_reason_code_approved_not_within_limits(self) -> None:
        """PRD 6.4 menulis `within_limits`; SQL hanya punya `approved`.

        Operator memutuskan SQL yang menang (2 Nov 2026).
        """
        decision = evaluate(request(), mandate(), snapshot(), NOW)
        assert decision.reason_code.value == "approved"
        assert not hasattr(ReasonCode, "WITHIN_LIMITS")

    def test_decision_carries_no_envelope(self) -> None:
        """Penandatanganan adalah M2; aturan tidak tahu soal kunci."""
        decision = evaluate(request(), mandate(), snapshot(), NOW)
        assert not hasattr(decision, "envelope")
        assert set(decision.__dataclass_fields__) == {"status", "reason_code", "retry_after"}


class TestEvaluationOrder:
    """Urutan PRD 6.4 bermakna: yang lebih awal menang."""

    def test_cooldown_wins_over_recipient_not_allowed(self) -> None:
        decision = evaluate(
            request(recipient=OUTSIDER),
            mandate(),
            snapshot(denial=denial_at(NOW - 1)),
            NOW,
        )
        assert decision.reason_code is ReasonCode.RETRY_BLOCKED

    def test_recipient_wins_over_per_tx_limit(self) -> None:
        decision = evaluate(request(1000 * USDC, OUTSIDER), mandate(), snapshot(), NOW)
        assert decision.reason_code is ReasonCode.RECIPIENT_NOT_ALLOWED

    def test_per_tx_wins_over_daily(self) -> None:
        decision = evaluate(request(30 * USDC), mandate(), snapshot(committed_day=100 * USDC), NOW)
        assert decision.reason_code is ReasonCode.OVER_PER_TX_LIMIT

    def test_daily_wins_over_human_approval(self) -> None:
        decision = evaluate(
            request(25 * USDC),
            mandate(),
            snapshot(committed_day=100 * USDC, committed_recipient=100 * USDC),
            NOW,
        )
        assert decision.reason_code is ReasonCode.OVER_DAILY_LIMIT


# --------------------------------------------------------------------------- #
# ST-11: cooldown
# --------------------------------------------------------------------------- #


class TestCooldownBoundaries:
    """ST-11: C-1 masih ditolak, C sudah lewat, C+1 sudah lewat."""

    def test_c_minus_one_is_still_blocked(self) -> None:
        denial = denial_at(NOW)
        ends = cooldown_ends_at(denial, mandate())
        decision = evaluate(request(), mandate(), snapshot(denial=denial), ends - 1)
        assert decision.status is Status.DENIED
        assert decision.reason_code is ReasonCode.RETRY_BLOCKED
        assert decision.retry_after == 1

    def test_at_c_the_cooldown_has_passed(self) -> None:
        """Batas inklusif di sisi boleh: tepat di C sudah lolos."""
        denial = denial_at(NOW)
        ends = cooldown_ends_at(denial, mandate())
        decision = evaluate(request(), mandate(), snapshot(denial=denial), ends)
        assert decision.status is Status.APPROVED

    def test_c_plus_one_has_passed(self) -> None:
        denial = denial_at(NOW)
        ends = cooldown_ends_at(denial, mandate())
        decision = evaluate(request(), mandate(), snapshot(denial=denial), ends + 1)
        assert decision.status is Status.APPROVED

    def test_retry_after_counts_down_exactly(self) -> None:
        denial = denial_at(NOW)
        ends = cooldown_ends_at(denial, mandate())
        for remaining in (COOLDOWN, COOLDOWN // 2, 2, 1):
            decision = evaluate(request(), mandate(), snapshot(denial=denial), ends - remaining)
            assert decision.retry_after == remaining

    def test_cooldown_ends_at_matches_the_specification(self) -> None:
        denial = denial_at(NOW)
        assert cooldown_ends_at(denial, mandate()) == NOW + COOLDOWN

    def test_evaluation_continues_after_cooldown(self) -> None:
        """Setelah cooldown lewat, langkah 2..6 berjalan seperti biasa."""
        denial = denial_at(NOW)
        ends = cooldown_ends_at(denial, mandate())
        decision = evaluate(request(recipient=OUTSIDER), mandate(), snapshot(denial=denial), ends)
        assert decision.reason_code is ReasonCode.RECIPIENT_NOT_ALLOWED


class TestCooldownTriggers:
    """ST-11: penolakan mana yang memulai cooldown, dan mana yang tidak."""

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
    def test_denied_triggers_cooldown(self, reason_code: ReasonCode) -> None:
        denial = denial_at(NOW, Status.DENIED, reason_code)
        decision = evaluate(request(), mandate(), snapshot(denial=denial), NOW + 1)
        assert decision.reason_code is ReasonCode.RETRY_BLOCKED

    def test_rejected_by_human_triggers_cooldown(self) -> None:
        denial = denial_at(NOW, Status.REJECTED_BY_HUMAN, ReasonCode.HUMAN_REJECTED)
        decision = evaluate(request(), mandate(), snapshot(denial=denial), NOW + 1)
        assert decision.reason_code is ReasonCode.RETRY_BLOCKED

    @pytest.mark.parametrize(
        ("status", "reason_code", "sebab"),
        [
            (Status.DENIED, ReasonCode.RETRY_BLOCKED, "retry_blocked tidak memperpanjang"),
            (Status.DENIED, ReasonCode.MANDATE_CHANGED, "mandate_changed (ST-09)"),
            (Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT, "expired"),
            (Status.APPROVED, ReasonCode.APPROVED, "approved bukan penolakan"),
            (Status.NEEDS_HUMAN, ReasonCode.HUMAN_APPROVAL_REQUIRED, "needs_human"),
        ],
    )
    def test_does_not_trigger_cooldown(
        self, status: Status, reason_code: ReasonCode, sebab: str
    ) -> None:
        """Baris yang bukan pemicu diabaikan, bahkan bila pemanggil mengirimnya."""
        denial = denial_at(NOW, status, reason_code)
        decision = evaluate(request(), mandate(), snapshot(denial=denial), NOW + 1)
        assert decision.status is Status.APPROVED, sebab

    def test_retry_blocked_does_not_extend_the_window(self) -> None:
        """Inti ST-11: jam berjalan dari penolakan PEMICU, bukan dari retry.

        Skenario: pemicu di T, lalu agent mencoba lagi di T+100 dan mendapat
        retry_blocked. Cooldown tetap berakhir di T+3600, bukan T+3700.
        """
        trigger = denial_at(NOW, Status.DENIED, ReasonCode.OVER_PER_TX_LIMIT)
        ends = cooldown_ends_at(trigger, mandate())
        assert ends == NOW + COOLDOWN

        # Masih diblokir satu detik sebelum jendela asli berakhir.
        assert (
            evaluate(request(), mandate(), snapshot(denial=trigger), ends - 1).reason_code
            is ReasonCode.RETRY_BLOCKED
        )
        # Dan lolos tepat di ujung jendela asli, walau ada percobaan di antara.
        assert evaluate(request(), mandate(), snapshot(denial=trigger), ends).status is (
            Status.APPROVED
        )

    def test_retry_blocked_row_does_not_shorten_the_window_either(self) -> None:
        """Sisi lain: baris retry_blocked yang lebih baru tidak memulai apa pun.

        Kalau pemanggil keliru mengirim baris retry_blocked, hasilnya adalah
        "tidak ada cooldown", bukan "cooldown baru dari waktu retry itu".
        """
        retry_row = denial_at(NOW + 100, Status.DENIED, ReasonCode.RETRY_BLOCKED)
        decision = evaluate(request(), mandate(), snapshot(denial=retry_row), NOW + 101)
        assert decision.status is Status.APPROVED

    def test_no_denial_means_no_cooldown(self) -> None:
        assert evaluate(request(), mandate(), snapshot(denial=None), NOW).status is (
            Status.APPROVED
        )

    def test_a_longer_cooldown_in_the_mandate_is_respected(self) -> None:
        denial = denial_at(NOW)
        long_mandate = mandate(retry_cooldown_seconds=7200)
        assert cooldown_ends_at(denial, long_mandate) == NOW + 7200
        decision = evaluate(request(), long_mandate, snapshot(denial=denial), NOW + 3700)
        assert decision.reason_code is ReasonCode.RETRY_BLOCKED


# --------------------------------------------------------------------------- #
# Allowlist dan batas
# --------------------------------------------------------------------------- #


class TestRecipientAllowlist:
    def test_outsider_is_denied(self) -> None:
        decision = evaluate(request(recipient=OUTSIDER), mandate(), snapshot(), NOW)
        assert decision.status is Status.DENIED
        assert decision.reason_code is ReasonCode.RECIPIENT_NOT_ALLOWED

    @pytest.mark.parametrize("recipient", [ALLOWED_A, ALLOWED_B])
    def test_listed_recipients_pass(self, recipient: str) -> None:
        decision = evaluate(request(recipient=recipient), mandate(), snapshot(), NOW)
        assert decision.status is Status.APPROVED


class TestPerTxLimit:
    def test_exactly_the_limit_passes(self) -> None:
        """Batas inklusif di sisi boleh."""
        decision = evaluate(request(25 * USDC), mandate(), snapshot(), NOW)
        assert decision.status is Status.APPROVED

    def test_one_base_unit_over_is_denied(self) -> None:
        decision = evaluate(request(25 * USDC + 1), mandate(), snapshot(), NOW)
        assert decision.status is Status.DENIED
        assert decision.reason_code is ReasonCode.OVER_PER_TX_LIMIT

    def test_one_base_unit_under_passes(self) -> None:
        decision = evaluate(request(25 * USDC - 1), mandate(), snapshot(), NOW)
        assert decision.status is Status.APPROVED


class TestDailyLimit:
    def test_exactly_the_limit_passes(self) -> None:
        decision = evaluate(request(25 * USDC), mandate(), snapshot(committed_day=75 * USDC), NOW)
        assert decision.status is Status.APPROVED

    def test_one_base_unit_over_is_denied(self) -> None:
        decision = evaluate(
            request(25 * USDC), mandate(), snapshot(committed_day=75 * USDC + 1), NOW
        )
        assert decision.status is Status.DENIED
        assert decision.reason_code is ReasonCode.OVER_DAILY_LIMIT

    def test_committed_day_counts_towards_the_limit(self) -> None:
        """Batas harian soal total, bukan soal satu permintaan."""
        small = request(1 * USDC)
        assert evaluate(small, mandate(), snapshot(committed_day=0), NOW).status is (
            Status.APPROVED
        )
        assert (
            evaluate(small, mandate(), snapshot(committed_day=100 * USDC), NOW).reason_code
            is ReasonCode.OVER_DAILY_LIMIT
        )


class TestHumanApprovalThreshold:
    """ST-12: agregasi per recipient."""

    def test_24_plus_24_crosses_25_and_needs_human(self) -> None:
        decision = evaluate(
            request(24 * USDC),
            mandate(),
            snapshot(committed_recipient=24 * USDC),
            NOW,
        )
        assert decision.status is Status.NEEDS_HUMAN
        assert decision.reason_code is ReasonCode.HUMAN_APPROVAL_REQUIRED

    def test_exactly_the_threshold_is_approved(self) -> None:
        """Total persis 25 tidak melewati ambang: inklusif di sisi boleh."""
        decision = evaluate(
            request(1 * USDC), mandate(), snapshot(committed_recipient=24 * USDC), NOW
        )
        assert decision.status is Status.APPROVED

    def test_one_base_unit_over_the_threshold_needs_human(self) -> None:
        decision = evaluate(
            request(1 * USDC), mandate(), snapshot(committed_recipient=24 * USDC + 1), NOW
        )
        assert decision.status is Status.NEEDS_HUMAN

    def test_aggregation_is_per_recipient_not_per_day(self) -> None:
        """committed_day tinggi tapi recipient ini masih bersih -> approved."""
        decision = evaluate(
            request(1 * USDC),
            mandate(),
            snapshot(committed_day=50 * USDC, committed_recipient=0),
            NOW,
        )
        assert decision.status is Status.APPROVED

    def test_single_large_payment_also_needs_human(self) -> None:
        decision = evaluate(request(25 * USDC), mandate(), snapshot(), NOW)
        assert decision.status is Status.APPROVED
        decision = evaluate(request(25 * USDC), mandate(human_approval_above="10"), snapshot(), NOW)
        assert decision.status is Status.NEEDS_HUMAN


# --------------------------------------------------------------------------- #
# Kemurnian
# --------------------------------------------------------------------------- #


class TestPurity:
    def test_signature_has_no_reason_parameter(self) -> None:
        """PRD 3.10 / ST-21: reason tidak boleh bisa memengaruhi keputusan.

        Dijamin oleh bentuk, bukan oleh disiplin: tidak ada parameternya.
        """
        parameters = set(inspect.signature(evaluate).parameters)
        assert parameters == {"request", "mandate", "state_snapshot", "now"}
        assert "reason" not in parameters

    def test_request_has_no_reason_field(self) -> None:
        assert set(Request.__dataclass_fields__) == {"recipient", "amount_base", "chain_id"}

    def test_request_has_no_agent_id_field(self) -> None:
        """Security v1.1 S1: identitas dari mandate, bukan dari permintaan."""
        assert "agent_id" not in Request.__dataclass_fields__

    def test_same_arguments_give_the_same_result(self) -> None:
        arguments = (request(), mandate(), snapshot(), NOW)
        first = evaluate(*arguments)
        second = evaluate(*arguments)
        assert first == second

    def test_repeated_calls_do_not_drift(self) -> None:
        arguments = (request(), mandate(), snapshot(denial=denial_at(NOW)), NOW + 1)
        results = {evaluate(*arguments) for _ in range(50)}
        assert len(results) == 1

    def test_only_retry_after_changes_when_now_moves(self) -> None:
        denial = denial_at(NOW)
        snap = snapshot(denial=denial)
        first = evaluate(request(), mandate(), snap, NOW + 1)
        second = evaluate(request(), mandate(), snap, NOW + 2)
        assert first.status == second.status
        assert first.reason_code == second.reason_code
        assert first.retry_after == second.retry_after + 1

    def test_arguments_are_not_mutated(self) -> None:
        req, man, snap = request(), mandate(), snapshot(denial=denial_at(NOW))
        before = (req, man, snap)
        evaluate(req, man, snap, NOW)
        assert (req, man, snap) == before

    def test_module_imports_nothing_impure(self) -> None:
        """Tanpa db, jaringan, atau jam. Arsitektur v1.1 bagian 12."""
        import ast
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "src" / "payment_guard" / "rules.py"
        ).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"psycopg", "socket", "requests", "time", "datetime", "random", "os"}
        assert not imported & forbidden, imported & forbidden


# --------------------------------------------------------------------------- #
# Invarian Decision
# --------------------------------------------------------------------------- #


class TestDecisionInvariants:
    @pytest.mark.parametrize(
        ("status", "reason_code"),
        [
            (Status.APPROVED, ReasonCode.OVER_DAILY_LIMIT),
            (Status.DENIED, ReasonCode.APPROVED),
            (Status.NEEDS_HUMAN, ReasonCode.HUMAN_REJECTED),
            (Status.EXPIRED, ReasonCode.APPROVED_BY_HUMAN),
        ],
    )
    def test_invalid_pair_cannot_be_constructed(
        self, status: Status, reason_code: ReasonCode
    ) -> None:
        """Pasangan tidak sah gagal saat konstruksi, bukan saat INSERT."""
        with pytest.raises(ReasonPairError):
            Decision(status, reason_code)

    def test_retry_blocked_requires_retry_after(self) -> None:
        with pytest.raises(ValueError, match="wajib membawa retry_after"):
            Decision(Status.DENIED, ReasonCode.RETRY_BLOCKED)

    def test_retry_after_only_for_retry_blocked(self) -> None:
        with pytest.raises(ValueError, match="hanya untuk retry_blocked"):
            Decision(Status.APPROVED, ReasonCode.APPROVED, retry_after=10)

    def test_retry_after_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="lebih besar dari nol"):
            Decision(Status.DENIED, ReasonCode.RETRY_BLOCKED, retry_after=0)

    def test_valid_decisions_construct(self) -> None:
        Decision(Status.APPROVED, ReasonCode.APPROVED)
        Decision(Status.DENIED, ReasonCode.RETRY_BLOCKED, retry_after=1)
        Decision(Status.NEEDS_HUMAN, ReasonCode.HUMAN_APPROVAL_REQUIRED)

    def test_decision_is_frozen(self) -> None:
        decision = Decision(Status.APPROVED, ReasonCode.APPROVED)
        with pytest.raises(AttributeError):
            decision.status = Status.DENIED  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Validasi masukan (langkah 0 PRD 6.4)
# --------------------------------------------------------------------------- #


class TestInputValidation:
    @pytest.mark.parametrize("amount", [0, -1, -1000])
    def test_non_positive_amount_rejected(self, amount: int) -> None:
        with pytest.raises(RuleInputError, match="amount_base"):
            evaluate(request(amount), mandate(), snapshot(), NOW)

    @pytest.mark.parametrize("amount", [1.0, "25", None, True])
    def test_non_int_amount_rejected(self, amount: object) -> None:
        with pytest.raises(RuleInputError, match="amount_base"):
            evaluate(
                Request(recipient=ALLOWED_A, amount_base=amount, chain_id=1),  # type: ignore[arg-type]
                mandate(),
                snapshot(),
                NOW,
            )

    @pytest.mark.parametrize("recipient", ["0x123", "", "bukan-alamat", None, 1])
    def test_bad_recipient_rejected(self, recipient: object) -> None:
        with pytest.raises(RuleInputError, match="recipient"):
            evaluate(
                Request(recipient=recipient, amount_base=USDC, chain_id=1),  # type: ignore[arg-type]
                mandate(),
                snapshot(),
                NOW,
            )

    def test_uppercase_recipient_rejected(self) -> None:
        """evaluate mengandaikan recipient sudah ternormalisasi lowercase."""
        with pytest.raises(RuleInputError, match="lowercase"):
            evaluate(
                request(recipient="0x" + ALLOWED_A[2:].upper()),
                mandate(),
                snapshot(),
                NOW,
            )

    @pytest.mark.parametrize("now", [0, -1])
    def test_non_positive_now_rejected(self, now: int) -> None:
        """Nol biasanya berarti jam database lupa disuntikkan."""
        with pytest.raises(RuleInputError, match="now"):
            evaluate(request(), mandate(), snapshot(), now)

    @pytest.mark.parametrize("now", [1.0, "123", None, True])
    def test_non_int_now_rejected(self, now: object) -> None:
        with pytest.raises(RuleInputError, match="now"):
            evaluate(request(), mandate(), snapshot(), now)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["committed_day", "committed_recipient"])
    def test_negative_committed_rejected(self, field: str) -> None:
        with pytest.raises(RuleInputError, match=field):
            evaluate(request(), mandate(), snapshot(**{field: -1}), NOW)

    @pytest.mark.parametrize("field", ["committed_day", "committed_recipient"])
    def test_non_int_committed_rejected(self, field: str) -> None:
        with pytest.raises(RuleInputError, match=field):
            evaluate(request(), mandate(), snapshot(**{field: 1.5}), NOW)

    def test_wrong_types_for_the_big_arguments_rejected(self) -> None:
        with pytest.raises(RuleInputError, match="request harus Request"):
            evaluate({"recipient": ALLOWED_A}, mandate(), snapshot(), NOW)  # type: ignore[arg-type]
        with pytest.raises(RuleInputError, match="mandate harus Mandate"):
            evaluate(request(), {"max_per_tx": 1}, snapshot(), NOW)  # type: ignore[arg-type]
        with pytest.raises(RuleInputError, match="state_snapshot harus Snapshot"):
            evaluate(request(), mandate(), {"committed_day": 0}, NOW)  # type: ignore[arg-type]

    def test_negative_denial_timestamp_rejected(self) -> None:
        with pytest.raises(RuleInputError, match="decided_at"):
            evaluate(request(), mandate(), snapshot(denial=denial_at(-1)), NOW)

    def test_input_errors_are_not_decisions(self) -> None:
        """PRD 6.4 langkah 0: gagal validasi adalah error, bukan Decision."""
        with pytest.raises(RuleInputError):
            evaluate(request(0), mandate(), snapshot(), NOW)
