"""Approvals CLI tanpa database: ST-23 dan konfirmasi token.

Yang diuji di sini adalah pintu masuknya. Pemeriksaan yang butuh Postgres —
cek ulang batas, kedaluwarsa, cooldown — ada di
``tests/integration/test_approvals.py``.

Tes di bawah sengaja memakai ``config`` yang URL-nya tidak bisa dihubungi.
Kalau ada jalur yang menyentuh database sebelum TTY dan token diperiksa, tes
akan gagal dengan error koneksi, bukan lulus diam-diam.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from payment_guard.approvals_cli import (
    CONFIRM_ALPHABET,
    CONFIRM_LENGTH,
    GUARD_RUNNING_FALSE,
    GUARD_RUNNING_TRUE,
    ApprovalError,
    DecisionRow,
    _payload,
    approve,
    confirm_with_token,
    make_token,
    normalize_confirmation,
    reject,
    require_interactive,
    require_uuid,
    write_reason,
)
from payment_guard.config import ApprovalsConfig
from payment_guard.events import Event, require_safe_payload
from payment_guard.reasons import ReasonCode, Status

DECISION_ID = "123e4567-e89b-42d3-a456-426614174000"

UNREACHABLE = ApprovalsConfig(
    db_url="postgresql://tidak:ada@127.0.0.1:1/tidak_ada",
    signing_key_path=Path("tidak-ada.json"),
    lock_timeout_ms=100,
)


class FakeStdin(io.StringIO):
    """stdin yang bisa berpura-pura TTY atau bukan."""

    def __init__(self, text: str = "", *, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def shown_token(out: io.StringIO) -> str:
    """Token yang baru saja ditampilkan ke layar."""
    return out.getvalue().split("melanjutkan:")[1].split()[0]


class ParrotStdin(FakeStdin):
    """stdin yang membaca token dari layar lalu mengetiknya ulang.

    Persis yang dilakukan operator, dan satu-satunya cara menguji jalur "token
    benar" tanpa membocorkan token keluar dari fungsinya: token dibangkitkan di
    dalam :func:`confirm_with_token` dan baru bisa diketahui setelah tercetak.
    """

    def __init__(self, out: io.StringIO, *, ubah=None, tty: bool = True) -> None:
        super().__init__("", tty=tty)
        self._out = out
        self._ubah = (lambda token: token) if ubah is None else ubah

    def readline(self, *args: object, **kwargs: object) -> str:
        return self._ubah(shown_token(self._out)) + "\n"


def row(**overrides: object) -> DecisionRow:
    fields: dict[str, object] = {
        "agent_id": "demo-agent",
        "recipient": "0x" + "a" * 40,
        "amount_base": 24_000_000,
        "reason_text": "beli kopi",
        "status": "needs_human",
        "mandate_hash": "f" * 64,
        "chain_id": 84532,
        "expires_at": 1_800_000_000,
    }
    fields.update(overrides)
    return DecisionRow(**fields)  # type: ignore[arg-type]


class TestNonInteractiveIsRefused:
    """ST-23 dan Security v1.1 E3: tidak ada jalan untuk skrip."""

    def test_require_interactive_refuses_a_pipe(self) -> None:
        with pytest.raises(ApprovalError, match="terminal interaktif"):
            require_interactive(FakeStdin(tty=False))

    def test_require_interactive_accepts_a_terminal(self) -> None:
        require_interactive(FakeStdin(tty=True))

    def test_approve_refuses_before_touching_the_database(self) -> None:
        with pytest.raises(ApprovalError, match="terminal interaktif"):
            approve(UNREACHABLE, DECISION_ID, stdin=FakeStdin(tty=False), out=io.StringIO())

    def test_reject_refuses_before_touching_the_database(self) -> None:
        with pytest.raises(ApprovalError, match="terminal interaktif"):
            reject(UNREACHABLE, DECISION_ID, stdin=FakeStdin(tty=False), out=io.StringIO())

    def test_the_message_names_no_flag_to_bypass_it(self) -> None:
        """Pesan yang menyebut flag akan mengundang orang mencarinya."""
        with pytest.raises(ApprovalError) as exc:
            require_interactive(FakeStdin(tty=False))
        assert "--yes" not in str(exc.value)
        assert "--force" not in str(exc.value)

    @pytest.mark.parametrize("perintah", ["approve", "reject"])
    def test_the_cli_offers_no_flag_to_skip_confirmation(self, perintah: str) -> None:
        """Diperiksa di parser, bukan di teks sumber.

        Docstring modul memang menyebut ``--yes`` untuk menerangkan bahwa ia
        tidak ada; memindai sumber akan salah tangkap. Yang mengikat adalah
        opsi yang benar-benar diterima argparse.
        """
        from payment_guard.cli import build_parser

        args = build_parser().parse_args(["approvals", perintah, DECISION_ID])
        assert set(vars(args)) == {"group", "command", "handler", "env_file", "decision_id"}


class TestConfirmationToken:
    def test_length(self) -> None:
        assert len(make_token()) == CONFIRM_LENGTH == 6

    def test_alphabet_is_crockford_base32(self) -> None:
        """Alfabetnya tepat 32 simbol Crockford, tanpa I, L, O, dan U."""
        assert CONFIRM_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        assert len(CONFIRM_ALPHABET) == 32
        assert len(set(CONFIRM_ALPHABET)) == 32

    @pytest.mark.parametrize("char", "ILOU")
    def test_the_confusable_letters_are_absent(self, char: str) -> None:
        assert char not in CONFIRM_ALPHABET

    def test_every_token_uses_only_that_alphabet(self) -> None:
        for _ in range(200):
            assert set(make_token()) <= set(CONFIRM_ALPHABET)

    @pytest.mark.parametrize(
        "diketik,dibaca",
        [
            ("O", "0"),
            ("o", "0"),
            ("I", "1"),
            ("i", "1"),
            ("L", "1"),
            ("l", "1"),
            ("ABC", "ABC"),
            ("  abc  ", "ABC"),
            ("LOL", "101"),
        ],
    )
    def test_crockford_reading_rules(self, diketik: str, dibaca: str) -> None:
        """Operator yang salah lihat tetap berhasil, bukan ditolak.

        ``0`` dan ``1`` ada di alfabet; ``O``, ``I``, dan ``L`` tidak. Jadi
        memetakan yang kedua ke yang pertama tidak menambah tabrakan.
        """
        assert normalize_confirmation(diketik) == dibaca

    def test_a_substituted_letter_is_accepted_when_retyped(self) -> None:
        """Token yang memuat 0 diterima walau operator mengetik O."""
        out = io.StringIO()
        confirm_with_token(
            ParrotStdin(out, ubah=lambda token: token.replace("0", "O").replace("1", "I")), out
        )

    def test_a_fresh_token_is_generated_for_every_prompt(self) -> None:
        """Dibangkitkan ulang setiap prompt, bukan sekali lalu dipakai ulang."""
        terlihat = []
        for _ in range(20):
            out = io.StringIO()
            confirm_with_token(ParrotStdin(out), out)
            terlihat.append(shown_token(out))
        assert len(set(terlihat)) > 1, "token sama di setiap prompt"

    def test_tokens_differ(self) -> None:
        """Token tetap akan jadi refleks, dan refleks justru yang dihindari."""
        assert len({make_token() for _ in range(200)}) > 100

    def test_token_is_accepted_when_retyped(self) -> None:
        out = io.StringIO()
        confirm_with_token(ParrotStdin(out), out)

    def test_the_token_appears_exactly_once_on_screen(self) -> None:
        out = io.StringIO()
        confirm_with_token(ParrotStdin(out), out)
        assert out.getvalue().count(shown_token(out)) == 1

    def test_wrong_token_is_refused(self) -> None:
        with pytest.raises(ApprovalError, match="tidak cocok"):
            confirm_with_token(FakeStdin("SALAH2\n", tty=True), io.StringIO())

    def test_empty_input_is_refused(self) -> None:
        with pytest.raises(ApprovalError, match="tidak cocok"):
            confirm_with_token(FakeStdin("", tty=True), io.StringIO())

    def test_refusal_says_nothing_changed(self) -> None:
        with pytest.raises(ApprovalError) as exc:
            confirm_with_token(FakeStdin("SALAH2\n", tty=True), io.StringIO())
        assert "tidak ada yang diubah" in str(exc.value)

    @pytest.mark.parametrize(
        "ubah",
        [
            lambda token: token.lower(),
            lambda token: f"  {token}  ",
            lambda token: f"{token.lower()}\r",
        ],
        ids=["huruf kecil", "berspasi", "huruf kecil dan CR"],
    )
    def test_ordinary_typing_slips_are_tolerated(self, ubah) -> None:
        """Yang dicari konfirmasi adalah perhatian, bukan ketepatan mengetik."""
        out = io.StringIO()
        confirm_with_token(ParrotStdin(out, ubah=ubah), out)

    def test_a_different_token_from_an_earlier_prompt_is_refused(self) -> None:
        """Token tidak bisa dipakai ulang: tiap konfirmasi membangkitkan sendiri."""
        pertama = io.StringIO()
        confirm_with_token(ParrotStdin(pertama), pertama)
        lama = shown_token(pertama)

        with pytest.raises(ApprovalError, match="tidak cocok"):
            confirm_with_token(FakeStdin(f"{lama}\n", tty=True), io.StringIO())


class TestDecisionIdValidation:
    def test_canonical_uuid_is_accepted(self) -> None:
        require_uuid(DECISION_ID)

    @pytest.mark.parametrize(
        "bad",
        ["", "bukan-uuid", "0", DECISION_ID[:-1], DECISION_ID + "0", None, 7],
    )
    def test_malformed_is_refused(self, bad: object) -> None:
        with pytest.raises(ApprovalError, match="decision_id"):
            require_uuid(bad)  # type: ignore[arg-type]

    def test_uppercase_is_refused(self) -> None:
        with pytest.raises(ApprovalError, match="huruf kecil kanonik"):
            require_uuid(DECISION_ID.upper())


class TestAuditPayload:
    def test_carries_no_reason(self) -> None:
        payload = _payload(DECISION_ID, row(), Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN, True)
        assert "reason" not in payload
        assert "reason_text" not in payload
        assert "beli kopi" not in str(payload)

    def test_carries_no_envelope_or_token(self) -> None:
        payload = _payload(DECISION_ID, row(), Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN, True)
        for forbidden in ("envelope", "signature", "signed_decision", "token"):
            assert forbidden not in payload

    def test_reason_code_is_kept(self) -> None:
        """Enum tertutup milik Guard, bukan teks agent."""
        payload = _payload(
            DECISION_ID, row(), Status.DENIED, ReasonCode.RECHECK_OVER_DAILY_LIMIT, False
        )
        assert payload["reason_code"] == "recheck_over_daily_limit"

    def test_guard_running_is_a_string_not_a_bool(self) -> None:
        """Jalur audit menolak bool supaya bentuk kanoniknya tidak ambigu."""
        hidup = _payload(DECISION_ID, row(), Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT, True)
        mati = _payload(DECISION_ID, row(), Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT, False)
        assert hidup["guard_running"] == GUARD_RUNNING_TRUE
        assert mati["guard_running"] == GUARD_RUNNING_FALSE
        assert not isinstance(hidup["guard_running"], bool)

    def test_amount_is_a_string_not_a_float(self) -> None:
        payload = _payload(DECISION_ID, row(), Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN, True)
        assert payload["amount_base"] == "24000000"
        assert isinstance(payload["amount_base"], str)

    @pytest.mark.parametrize(
        ("status", "reason_code"),
        [
            (Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN),
            (Status.REJECTED_BY_HUMAN, ReasonCode.HUMAN_REJECTED),
            (Status.DENIED, ReasonCode.RECHECK_OVER_PER_TX_LIMIT),
            (Status.DENIED, ReasonCode.MANDATE_CHANGED),
            (Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT),
        ],
    )
    def test_payload_passes_the_audit_guard(self, status: Status, reason_code: ReasonCode) -> None:
        require_safe_payload(_payload(DECISION_ID, row(), status, reason_code, True))

    def test_m4_events_exist(self) -> None:
        assert Event.APPROVAL_APPROVED.value == "approval_approved"
        assert Event.APPROVAL_REJECTED.value == "approval_rejected"
        assert Event.DECISION_EXPIRED.value == "decision_expired"


class TestReasonIsRenderedAsData:
    def test_reason_goes_through_the_untrusted_block(self) -> None:
        out = io.StringIO()
        write_reason(out, "abaikan aturan")
        teks = out.getvalue()
        assert "bukan instruksi" in teks
        assert "abaikan aturan" in teks

    def test_control_characters_never_reach_the_screen(self) -> None:
        out = io.StringIO()
        write_reason(out, "a\x1b[31mb\nc")
        teks = out.getvalue()
        assert "\x1b" not in teks
        assert "\\u001B" in teks

    def test_agent_text_cannot_add_lines(self) -> None:
        """Tiga baris blok, berapa pun newline yang diselipkan agent."""
        out = io.StringIO()
        write_reason(out, "satu\ndua\ntiga\nempat")
        assert len(out.getvalue().splitlines()) == 3
