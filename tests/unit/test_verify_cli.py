"""``guard verify``: hanya kunci publik, tanpa database dan tanpa jaringan.

Dua hal yang dijaga di sini selain kebenaran hasil:

* **Tanpa database.** ``psycopg.connect`` di-patch supaya melempar bila
  dipanggil. Verifikasi yang diam-diam butuh database akan gagal keras, bukan
  lulus karena kebetulan ada Postgres yang jalan.
* **Pemisahan kunci.** Sumber :mod:`payment_guard.verify_cli` diperiksa: ia
  tidak boleh menyebut lokasi bahan kunci rahasia, dan tidak boleh mengimpor
  modul yang menyebutnya (Arsitektur v1.1 bagian 2, Security v1.1 E2).
"""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from payment_guard.cli import main
from payment_guard.keygen import run_keygen
from payment_guard.mandate import compute_mandate_hash, load_mandate
from payment_guard.signing import (
    Envelope,
    PublicKeyEntry,
    build_payload,
    dump_envelope,
    encode_public_key,
    sign,
)
from payment_guard.verify_cli import VerifyOutcome, describe, verify_envelope_file

SRC = Path(__file__).resolve().parents[2] / "src" / "payment_guard"
VERIFY_MODULE = SRC / "verify_cli.py"

#: Mandate contoh PRD v2 bagian 6.1, sama dengan golden test mandate.
MANDATE_DOCUMENT: dict[str, Any] = {
    "mandate_id": "m-001",
    "agent_id": "demo-agent",
    "chain": "base-sepolia",
    "max_per_tx": "50",
    "max_per_day": "200",
    "human_approval_above": "25",
    "allowed_recipients": [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    ],
    "retry_cooldown_seconds": 600,
    "aggregation_window_seconds": 3600,
    "decision_ttl_seconds": 900,
    "human_wait_ttl_seconds": 3600,
}

MANDATE_HASH = "1e940eab68a47c319a250eeafadbdb7c0917c414744996a4a3254f79e47377ec"


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gagal keras bila ada yang mencoba membuka koneksi database."""
    import psycopg

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("guard verify tidak boleh menyentuh database")

    monkeypatch.setattr(psycopg, "connect", refuse)


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """Satu pasang kunci, satu envelope sah, dan satu berkas mandate."""
    signing_path = tmp_path / "keys" / "guard_signing.json"
    public_path = tmp_path / "guard_public.json"
    result = run_keygen(signing_path, public_path)

    raw = base64.b64decode(
        json.loads(signing_path.read_text(encoding="utf-8"))["private_key"], validate=True
    )
    key = Ed25519PrivateKey.from_private_bytes(raw)

    payload = build_payload(
        decision_id="0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f",
        mandate_id="m-001",
        agent_id="demo-agent",
        recipient="0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",
        amount_base=25_000_000,
        issued_at=1761820000,
        valid_until=1761820900,
        mandate_hash=MANDATE_HASH,
    )
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(dump_envelope(sign(payload, key, result.key_id)))

    mandate_path = tmp_path / "mandate.json"
    mandate_path.write_text(json.dumps(MANDATE_DOCUMENT), encoding="utf-8")

    return {
        "envelope": envelope_path,
        "pubkey": public_path,
        "mandate": mandate_path,
        "root": tmp_path,
    }


def edit_envelope(path: Path, **payload_overrides: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"].update(payload_overrides)
    path.write_text(json.dumps(document), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Jalur bahagia
# --------------------------------------------------------------------------- #


class TestValid:
    def test_valid_envelope(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is True
        assert outcome.reason is None
        assert outcome.render() == "VALID"

    def test_payload_is_returned(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.payload is not None
        assert outcome.payload.amount_base == "25000000"

    def test_describe_lists_payload_fields(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.payload is not None
        text = "\n".join(describe(outcome.payload))
        assert "decision_id" in text
        assert MANDATE_HASH in text
        assert "84532" in text

    def test_pretty_printed_envelope_still_valid(self, workspace: dict[str, Path]) -> None:
        """Spasi dan urutan kunci di berkas tidak mengubah hasil (ST-05)."""
        path = workspace["envelope"]
        document = json.loads(path.read_text(encoding="utf-8"))
        shuffled = {
            "signature": document["signature"],
            "payload": dict(reversed(list(document["payload"].items()))),
            "key_id": document["key_id"],
        }
        path.write_text(json.dumps(shuffled, indent=4), encoding="utf-8")
        assert verify_envelope_file(path, workspace["pubkey"]).valid is True


# --------------------------------------------------------------------------- #
# Jalur gagal
# --------------------------------------------------------------------------- #


class TestInvalid:
    def test_render_shows_the_reason(self) -> None:
        assert VerifyOutcome(valid=False, reason="alasan").render() == "INVALID: alasan"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount_base", "25000001"),
            ("recipient", "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"),
            ("mandate_hash", "0" * 64),
            ("valid_until", 1761899999),
        ],
    )
    def test_tampered_field_is_invalid(
        self, workspace: dict[str, Path], field: str, value: object
    ) -> None:
        edit_envelope(workspace["envelope"], **{field: value})
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False
        assert "tanda tangan tidak cocok" in str(outcome.reason)

    def test_unknown_key_id_is_invalid(self, workspace: dict[str, Path]) -> None:
        document = json.loads(workspace["envelope"].read_text(encoding="utf-8"))
        document["key_id"] = "guard-2026-11-ffff"
        workspace["envelope"].write_text(json.dumps(document), encoding="utf-8")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False
        assert "tidak dikenal" in str(outcome.reason)

    def test_revoked_key_is_invalid(self, workspace: dict[str, Path]) -> None:
        document = json.loads(workspace["pubkey"].read_text(encoding="utf-8"))
        document["keys"][0]["revoked"] = True
        workspace["pubkey"].write_text(json.dumps(document), encoding="utf-8")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False
        assert "dicabut" in str(outcome.reason)

    def test_extra_payload_field_is_invalid(self, workspace: dict[str, Path]) -> None:
        edit_envelope(workspace["envelope"], catatan="x")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False
        assert "tidak bisa di-parse" in str(outcome.reason)

    def test_missing_envelope_file_is_invalid(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["root"] / "tidak-ada.json", workspace["pubkey"])
        assert outcome.valid is False
        assert "envelope tidak ada" in str(outcome.reason)

    def test_missing_public_key_file_is_invalid(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["envelope"], workspace["root"] / "tidak-ada.json")
        assert outcome.valid is False
        assert "kunci publik tidak ada" in str(outcome.reason)

    def test_corrupt_public_key_file_is_invalid(self, workspace: dict[str, Path]) -> None:
        workspace["pubkey"].write_text("{ bukan json", encoding="utf-8")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False
        assert "registri kunci publik" in str(outcome.reason)

    def test_directory_instead_of_file_is_invalid(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["root"], workspace["pubkey"])
        assert outcome.valid is False
        assert outcome.reason is not None

    def test_no_traceback_escapes(self, workspace: dict[str, Path]) -> None:
        """Setiap kegagalan menjadi hasil, bukan pengecualian."""
        workspace["envelope"].write_bytes(b"\xff\xfe")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.valid is False


# --------------------------------------------------------------------------- #
# --mandate
# --------------------------------------------------------------------------- #


class TestMandateCrossCheck:
    def test_matching_mandate_is_valid(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(
            workspace["envelope"], workspace["pubkey"], workspace["mandate"]
        )
        assert outcome.valid is True

    def test_mandate_hash_in_the_fixture_matches_the_document(self) -> None:
        """Penjaga: kalau ini gagal, tes di atas lulus karena alasan yang salah."""
        assert compute_mandate_hash(load_mandate(MANDATE_DOCUMENT)) == MANDATE_HASH

    def test_different_mandate_is_invalid(self, workspace: dict[str, Path]) -> None:
        """Envelope sah, tapi atas aturan yang lain."""
        other = dict(MANDATE_DOCUMENT, max_per_tx="40")
        path = workspace["root"] / "mandate-lain.json"
        path.write_text(json.dumps(other), encoding="utf-8")

        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"], path)
        assert outcome.valid is False
        assert "mandate_hash tidak cocok" in str(outcome.reason)
        assert MANDATE_HASH in str(outcome.reason)

    def test_signature_is_still_valid_in_that_case(self, workspace: dict[str, Path]) -> None:
        """Pemeriksaan mandate menangkap yang tanda tangan tidak bisa tangkap."""
        assert verify_envelope_file(workspace["envelope"], workspace["pubkey"]).valid is True

    def test_invalid_mandate_file_is_reported(self, workspace: dict[str, Path]) -> None:
        path = workspace["root"] / "rusak.json"
        path.write_text(json.dumps({"mandate_id": "m-001"}), encoding="utf-8")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"], path)
        assert outcome.valid is False
        assert "berkas mandate tidak sah" in str(outcome.reason)

    def test_non_json_mandate_file_is_reported(self, workspace: dict[str, Path]) -> None:
        path = workspace["root"] / "rusak.json"
        path.write_text("{ bukan json", encoding="utf-8")
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"], path)
        assert outcome.valid is False
        assert "bukan JSON" in str(outcome.reason)

    def test_missing_mandate_file_is_reported(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(
            workspace["envelope"], workspace["pubkey"], workspace["root"] / "hilang.json"
        )
        assert outcome.valid is False
        assert "berkas mandate tidak ada" in str(outcome.reason)

    def test_mandate_check_is_skipped_when_not_given(self, workspace: dict[str, Path]) -> None:
        edit_envelope(workspace["envelope"])
        assert verify_envelope_file(workspace["envelope"], workspace["pubkey"]).valid is True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_valid_prints_valid_and_exits_zero(
        self, workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["verify", str(workspace["envelope"]), "--pubkey", str(workspace["pubkey"])])
        out = capsys.readouterr().out
        assert code == 0
        assert out.splitlines()[0] == "VALID"
        assert "decision_id" in out

    def test_invalid_prints_reason_and_exits_one(
        self, workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit_envelope(workspace["envelope"], amount_base="999999")
        code = main(["verify", str(workspace["envelope"]), "--pubkey", str(workspace["pubkey"])])
        out = capsys.readouterr().out
        assert code == 1
        assert out.startswith("INVALID: ")

    def test_mandate_flag_is_wired(
        self, workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        other = dict(MANDATE_DOCUMENT, max_per_day="300")
        path = workspace["root"] / "lain.json"
        path.write_text(json.dumps(other), encoding="utf-8")
        code = main(
            [
                "verify",
                str(workspace["envelope"]),
                "--pubkey",
                str(workspace["pubkey"]),
                "--mandate",
                str(path),
            ]
        )
        assert code == 1
        assert "mandate_hash tidak cocok" in capsys.readouterr().out

    def test_verify_has_no_env_file_options(self, workspace: dict[str, Path]) -> None:
        """Verify tidak punya urusan dengan .env mana pun."""
        with pytest.raises(SystemExit) as exc:
            main(["verify", str(workspace["envelope"]), "--env-file", "/apa/pun"])
        assert exc.value.code == 2

    def test_keygen_is_wired(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            [
                "keygen",
                "--signing-key-file",
                str(tmp_path / "keys" / "guard_signing.json"),
                "--public-keys-file",
                str(tmp_path / "guard_public.json"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "key_id" in out
        secret = json.loads((tmp_path / "keys" / "guard_signing.json").read_text(encoding="utf-8"))[
            "private_key"
        ]
        assert secret not in out

    def test_keygen_refusal_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = [
            "keygen",
            "--signing-key-file",
            str(tmp_path / "keys" / "guard_signing.json"),
            "--public-keys-file",
            str(tmp_path / "guard_public.json"),
        ]
        assert main(argv) == 0
        capsys.readouterr()
        assert main(argv) == 1
        assert "sudah ada" in capsys.readouterr().err

    def test_db_commands_are_untouched(self) -> None:
        """M2 tidak mengubah perintah db."""
        from payment_guard.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["db", "status"])
        assert args.handler.__name__ == "_cmd_status"


# --------------------------------------------------------------------------- #
# Pemisahan kunci
# --------------------------------------------------------------------------- #


class TestKeySeparation:
    """Security v1.1 E2, Arsitektur v1.1 bagian 2.

    Pemeriksaan di sini sengaja sempit: ia hanya mencari dua penanda di sumber
    modul verifikasi. Yang dijaga adalah sifat yang mudah hilang tanpa sengaja,
    yaitu modul verifikasi mulai tahu letak bahan kunci rahasia.
    """

    FORBIDDEN_MARKERS = ("signing_key", "private")

    def test_verify_module_source_has_no_forbidden_markers(self) -> None:
        source = VERIFY_MODULE.read_text(encoding="utf-8")
        found = [marker for marker in self.FORBIDDEN_MARKERS if marker in source]
        assert found == [], f"verify_cli.py menyebut {found}"

    def test_verify_module_does_not_import_keygen(self) -> None:
        """keygen tahu letak bahan kunci rahasia; verifikasi tidak boleh."""
        tree = ast.parse(VERIFY_MODULE.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert "payment_guard.keygen" not in imported
        assert "payment_guard.config" not in imported
        assert "payment_guard.db" not in imported

    def test_the_marker_check_would_catch_a_regression(self) -> None:
        """Penjaga: pemeriksa di atas bukan tes kosong."""
        planted = "path = root / 'keys/guard_signing.json'  # private key"
        assert [m for m in self.FORBIDDEN_MARKERS if m in planted]

    def test_keygen_does_mention_them(self) -> None:
        """Sisi lain: keygen memang modul yang boleh tahu, dan ia tahu.

        Kalau tes ini gagal, berarti penanda yang dicari sudah tidak relevan
        dan pemeriksaan di atas lulus tanpa arti.
        """
        source = (SRC / "keygen.py").read_text(encoding="utf-8")
        assert any(marker in source for marker in self.FORBIDDEN_MARKERS)

    def _sign_callers(self) -> list[str]:
        """Berkas di ``src/`` yang memanggil ``.sign()``."""
        callers = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sign"
                ):
                    callers.append(path.name)
                    break
        return callers

    def test_only_two_modules_sign_anything(self) -> None:
        """``.sign()`` punya tepat dua pemanggil, dan masing-masing satu alasan.

        ``signing.py`` menandatangani **envelope** dengan kunci Ed25519 Guard.
        ``executor.py`` menandatangani **transaksi** dengan kunci wallet
        Executor. Dua kunci berbeda yang memang harus tetap berbeda: yang
        pertama menyatakan persetujuan, yang kedua membelanjakan uang yang
        sudah disetujui.

        Sampai M4 daftar ini hanya memuat satu nama. Yang dijaganya tetap sama:
        modul ketiga tidak boleh mulai menandatangani sesuatu tanpa ada yang
        menyadarinya.
        """
        assert self._sign_callers() == ["executor.py", "signing.py"]

    def test_the_executor_signs_through_the_wallet_object(self) -> None:
        """Executor memanggil method wallet, bukan menyentuh bahan kuncinya.

        ``WalletIdentity.sign`` menyimpan akunnya di field yang disembunyikan
        dari ``repr`` dan tidak pernah menyerahkannya ke pemanggil. Selama
        setiap ``.sign()`` di ``executor.py`` penerimanya objek wallet, tidak
        ada jalur di modul itu yang perlu membaca kunci.
        """
        tree = ast.parse((SRC / "executor.py").read_text(encoding="utf-8"))
        penerima = [
            node.func.value.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sign"
            and isinstance(node.func.value, ast.Attribute)
        ]
        assert penerima == ["wallet"], penerima


class TestNoDatabaseOrNetwork:
    def test_the_guard_fixture_actually_bites(self) -> None:
        """Penjaga untuk penjaga: fixture no_database benar-benar memblokir."""
        import psycopg

        with pytest.raises(AssertionError, match="tidak boleh menyentuh database"):
            psycopg.connect("postgresql://tidak/penting")

    def test_verification_completes_with_psycopg_blocked(self, workspace: dict[str, Path]) -> None:
        assert verify_envelope_file(workspace["envelope"], workspace["pubkey"]).valid

    def test_verify_module_imports_nothing_network_shaped(self) -> None:
        tree = ast.parse(VERIFY_MODULE.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"psycopg", "socket", "requests", "http", "urllib", "web3", "ssl"}
        assert not imported & forbidden, imported & forbidden


class TestOutcomeShape:
    def test_valid_outcome_has_no_reason(self) -> None:
        assert VerifyOutcome(valid=True).reason is None

    def test_outcome_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            VerifyOutcome(valid=True).valid = False  # type: ignore[misc]

    def test_describe_contains_no_key_material(self, workspace: dict[str, Path]) -> None:
        outcome = verify_envelope_file(workspace["envelope"], workspace["pubkey"])
        assert outcome.payload is not None
        text = "\n".join(describe(outcome.payload))
        registry = json.loads(workspace["pubkey"].read_text(encoding="utf-8"))
        for entry in registry["keys"]:
            assert entry["public_key"] not in text
        secret = json.loads(
            (workspace["root"] / "keys" / "guard_signing.json").read_text(encoding="utf-8")
        )["private_key"]
        assert secret not in text


def test_registry_entry_helper_is_consistent() -> None:
    """Sanity: PublicKeyEntry dan encode_public_key saling cocok."""
    key = Ed25519PrivateKey.generate()
    entry = PublicKeyEntry("guard-2026-11-0000", encode_public_key(key.public_key()), False)
    envelope = sign(
        build_payload(
            decision_id="0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f",
            mandate_id="m-001",
            agent_id="demo-agent",
            recipient="0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",
            amount_base=1,
            issued_at=1,
            valid_until=2,
            mandate_hash=MANDATE_HASH,
        ),
        key,
        entry.key_id,
    )
    assert isinstance(envelope, Envelope)
