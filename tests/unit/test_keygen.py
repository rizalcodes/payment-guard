"""``guard keygen``: pembuatan kunci, penolakan penimpaan, dan kerahasiaan.

Semua berkas ditulis ke ``tmp_path``. Tidak ada tes di sini yang menyentuh
``keys/`` atau ``guard_public.json`` milik repo.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from payment_guard.keygen import (
    PUBLIC_KEYS_RELATIVE_PATH,
    SIGNING_KEY_RELATIVE_PATH,
    KeygenError,
    new_key_id,
    run_keygen,
)
from payment_guard.signing import (
    KEY_ID_PATTERN,
    Payload,
    build_payload,
    decode_public_key,
    encode_public_key,
    parse_public_keys,
    sign,
    verify,
)


def payload() -> Payload:
    """Payload uji yang berdiri sendiri.

    Tidak diimpor dari ``test_signing`` supaya kedua berkas tes bisa dibaca
    dan dijalankan terpisah.
    """
    return build_payload(
        decision_id="0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f",
        mandate_id="m-001",
        agent_id="demo-agent",
        recipient="0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",
        amount_base=25_000_000,
        issued_at=1761820000,
        valid_until=1761820900,
        mandate_hash="1e940eab68a47c319a250eeafadbdb7c0917c414744996a4a3254f79e47377ec",
    )


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "keys" / "guard_signing.json", tmp_path / "guard_public.json"


def secret_material(path: Path) -> str:
    """Ambil bahan kunci rahasia dari berkas, untuk dipakai sebagai canary."""
    return json.loads(path.read_text(encoding="utf-8"))["private_key"]


class TestKeyIdShape:
    def test_matches_the_documented_example_shape(self) -> None:
        """PRD v2 bagian 7 memakai bentuk ``guard-2026-10``, plus sufiks acak."""
        key_id = new_key_id(datetime(2026, 11, 2, tzinfo=UTC))
        assert re.fullmatch(r"guard-2026-11-[0-9a-f]{4}", key_id), key_id

    def test_is_accepted_by_the_signing_pattern(self) -> None:
        assert KEY_ID_PATTERN.match(new_key_id()) is not None

    def test_two_calls_differ(self) -> None:
        """Sufiks acak mencegah tabrakan di bulan yang sama."""
        assert len({new_key_id() for _ in range(50)}) > 1

    def test_uses_the_given_moment(self) -> None:
        assert new_key_id(datetime(2027, 1, 9, tzinfo=UTC)).startswith("guard-2027-01-")


class TestFirstRun:
    def test_writes_both_files(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        assert signing_path.is_file()
        assert public_path.is_file()
        assert result.key_id.startswith("guard-")
        assert result.public_keys_total == 1

    def test_creates_the_parent_directory(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        assert not signing_path.parent.exists()
        run_keygen(signing_path, public_path)
        assert signing_path.parent.is_dir()

    def test_public_file_is_a_valid_registry(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        registry = parse_public_keys(public_path.read_bytes())
        assert set(registry) == {result.key_id}
        assert registry[result.key_id].revoked is False

    def test_new_key_is_not_revoked(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        document = json.loads(public_path.read_text(encoding="utf-8"))
        assert document["keys"][0]["revoked"] is False
        assert document["keys"][0]["key_id"] == result.key_id

    def test_key_id_matches_in_both_files(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        secret_document = json.loads(signing_path.read_text(encoding="utf-8"))
        assert secret_document["key_id"] == result.key_id

    def test_algorithm_is_recorded(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        assert json.loads(signing_path.read_text(encoding="utf-8"))["algorithm"] == "ed25519"

    def test_generated_pair_actually_works(self, paths: tuple[Path, Path]) -> None:
        """Bukti ujung ke ujung: tandatangani dengan rahasia, verifikasi dengan publik."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)

        raw = base64.b64decode(secret_material(signing_path), validate=True)
        key = Ed25519PrivateKey.from_private_bytes(raw)
        registry = parse_public_keys(public_path.read_bytes())

        envelope = sign(payload(), key, result.key_id)
        assert verify(envelope, registry).decision_id == payload().decision_id

    def test_public_key_in_file_matches_the_secret(self, paths: tuple[Path, Path]) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        raw = base64.b64decode(secret_material(signing_path), validate=True)
        derived = encode_public_key(Ed25519PrivateKey.from_private_bytes(raw).public_key())
        registry = parse_public_keys(public_path.read_bytes())
        assert registry[result.key_id].public_key == derived


class TestNeverOverwrites:
    def test_second_run_refuses(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        with pytest.raises(KeygenError, match="sudah ada"):
            run_keygen(signing_path, public_path)

    def test_second_run_leaves_the_secret_untouched(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        before = signing_path.read_bytes()
        with pytest.raises(KeygenError):
            run_keygen(signing_path, public_path)
        assert signing_path.read_bytes() == before

    def test_second_run_leaves_the_public_file_untouched(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        before = public_path.read_bytes()
        with pytest.raises(KeygenError):
            run_keygen(signing_path, public_path)
        assert public_path.read_bytes() == before

    def test_refusal_points_at_the_rotation_runbook(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        with pytest.raises(KeygenError) as exc:
            run_keygen(signing_path, public_path)
        message = str(exc.value)
        assert "runbook 8.1" in message
        assert "revoked" in message

    def test_refusal_does_not_leak_key_material(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        material = secret_material(signing_path)
        with pytest.raises(KeygenError) as exc:
            run_keygen(signing_path, public_path)
        assert material not in str(exc.value)

    def test_even_an_empty_existing_file_blocks_it(self, paths: tuple[Path, Path]) -> None:
        """Berkas kosong bisa berarti penulisan yang terputus; jangan ditimpa."""
        signing_path, public_path = paths
        signing_path.parent.mkdir(parents=True)
        signing_path.write_text("", encoding="utf-8")
        with pytest.raises(KeygenError, match="sudah ada"):
            run_keygen(signing_path, public_path)


class TestRotationAppends:
    def test_existing_entries_are_kept(self, paths: tuple[Path, Path]) -> None:
        """Envelope lama masih bisa diverifikasi sampai operator mencabutnya."""
        signing_path, public_path = paths
        first = run_keygen(signing_path, public_path)

        # Rotasi: operator memindahkan berkas rahasia lama.
        signing_path.rename(signing_path.with_suffix(".lama"))
        second = run_keygen(signing_path, public_path)

        registry = parse_public_keys(public_path.read_bytes())
        assert set(registry) == {first.key_id, second.key_id}
        assert second.public_keys_total == 2

    def test_revoked_flags_are_preserved(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        first = run_keygen(signing_path, public_path)

        document = json.loads(public_path.read_text(encoding="utf-8"))
        document["keys"][0]["revoked"] = True
        public_path.write_text(json.dumps(document), encoding="utf-8")

        signing_path.rename(signing_path.with_suffix(".lama"))
        run_keygen(signing_path, public_path)

        registry = parse_public_keys(public_path.read_bytes())
        assert registry[first.key_id].revoked is True

    def test_corrupt_public_file_fails_before_writing_a_secret(
        self, paths: tuple[Path, Path]
    ) -> None:
        signing_path, public_path = paths
        public_path.write_text("{ bukan json", encoding="utf-8")
        with pytest.raises(KeygenError, match="registri kunci"):
            run_keygen(signing_path, public_path)
        assert not signing_path.exists(), "kunci rahasia ditulis padahal keygen gagal"


class TestSecrecy:
    def test_secret_never_reaches_stdout_or_stderr(
        self, paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        material = secret_material(signing_path)

        for line in result.summary_lines():
            print(line)
        captured = capsys.readouterr()

        assert material not in captured.out
        assert material not in captured.err
        assert result.key_id in captured.out

    def test_run_keygen_prints_nothing_by_itself(
        self, paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_result_object_carries_no_key_material(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        material = secret_material(signing_path)
        assert material not in repr(result)
        assert not [f for f in result.__dataclass_fields__ if "private" in f]

    def test_public_file_has_no_secret_material(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        text = public_path.read_text(encoding="utf-8")
        assert "private" not in text
        assert secret_material(signing_path) not in text

    def test_public_file_holds_only_32_byte_public_keys(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        for entry in json.loads(public_path.read_text(encoding="utf-8"))["keys"]:
            assert len(base64.b64decode(entry["public_key"], validate=True)) == 32
            decode_public_key(entry["public_key"])

    def test_summary_mentions_both_paths_and_the_key_id(self, paths: tuple[Path, Path]) -> None:
        signing_path, public_path = paths
        result = run_keygen(signing_path, public_path)
        text = "\n".join(result.summary_lines())
        assert result.key_id in text
        assert str(signing_path) in text
        assert str(public_path) in text

    def test_secret_file_permissions_are_attempted(self, paths: tuple[Path, Path]) -> None:
        """Mode 0600 diminta saat pembuatan.

        Di Windows mode POSIX tidak dipetakan, jadi yang bisa diuji di sini
        hanyalah bahwa berkasnya ada dan tidak bisa ditimpa. Batas itu dicatat
        di docstring modul keygen dan di Security v1.1 RR-1.
        """
        import os

        signing_path, public_path = paths
        run_keygen(signing_path, public_path)
        mode = signing_path.stat().st_mode & 0o777
        if os.name == "posix":
            assert mode == 0o600
        else:
            assert signing_path.is_file()


class TestDefaultPaths:
    def test_relative_paths_are_the_documented_ones(self) -> None:
        assert SIGNING_KEY_RELATIVE_PATH == "keys/guard_signing.json"
        assert PUBLIC_KEYS_RELATIVE_PATH == "guard_public.json"

    def test_secret_lives_under_the_gitignored_directory(self) -> None:
        assert SIGNING_KEY_RELATIVE_PATH.startswith("keys/")
