"""Lingkungan eval: penjaga cluster dan hash mandate.

Tes hash mandate di sini ada karena versi pertama ``evals/environment.py``
menghitung hashnya sendiri dan **salah**: ia melewatkan awalan domain
``MANDATE_HASH_PREFIX``. Hash yang hampir benar lebih buruk daripada tidak
ada hash — E4 membandingkan ``active_mandates`` dengan nilai itu, dan
perbandingan yang selalu gagal akan terbaca sebagai mandate yang berganti,
yaitu persis kegagalan yang E4 cari.

Jadi tesnya tidak membandingkan dengan konstanta yang ditulis tangan. Ia
membandingkan dengan jalur yang **Guard sendiri** pakai saat start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.environment import EnvironmentError_, mandate_hash_of, require_test_cluster
from evals.scenarios import EVAL_MANDATE
from payment_guard.guard_service import load_mandate_file
from payment_guard.mandate import MANDATE_HASH_PREFIX, compute_mandate_hash, normalized_bytes


class TestMandateHashMatchesGuard:
    """Hash eval harus identik dengan hash Guard untuk mandate yang sama."""

    def test_matches_guard_startup_path(self, tmp_path: Path) -> None:
        """Jalur Guard: baca berkas, lalu ``compute_mandate_hash``.

        Inilah urutan di ``guard_service.start_guard``. Membandingkan dengan
        itu, bukan dengan nilai harapan yang ditulis tangan, berarti tesnya
        tetap benar kalau rumus hash Guard berubah — dan tetap gagal kalau
        eval menyimpang darinya.
        """
        berkas = tmp_path / "mandate.json"
        berkas.write_text(json.dumps(EVAL_MANDATE, indent=2, sort_keys=True), encoding="utf-8")

        guard = compute_mandate_hash(load_mandate_file(berkas))

        assert mandate_hash_of(EVAL_MANDATE) == guard

    def test_includes_domain_prefix(self, tmp_path: Path) -> None:
        """Hash tanpa awalan domain harus **berbeda**.

        Ini tes anti-regresi untuk cacat yang sudah pernah terjadi. Kalau
        suatu hari ``mandate_hash_of`` kembali menghitung SHA-256 polos,
        tes di atas masih bisa lolos seandainya Guard juga berubah; tes ini
        tidak bisa.
        """
        import hashlib

        from payment_guard.mandate import load_mandate

        mandate = load_mandate(EVAL_MANDATE)
        tanpa_awalan = hashlib.sha256(normalized_bytes(mandate)).hexdigest()
        dengan_awalan = hashlib.sha256(MANDATE_HASH_PREFIX + normalized_bytes(mandate)).hexdigest()

        assert tanpa_awalan != dengan_awalan
        assert mandate_hash_of(EVAL_MANDATE) == dengan_awalan

    def test_is_stable(self) -> None:
        """Hash yang sama untuk dokumen yang sama, dipanggil dua kali."""
        assert mandate_hash_of(EVAL_MANDATE) == mandate_hash_of(EVAL_MANDATE)

    def test_is_sixtyfour_hex(self) -> None:
        hasil = mandate_hash_of(EVAL_MANDATE)
        assert len(hasil) == 64
        assert all(c in "0123456789abcdef" for c in hasil)


class TestTestClusterGuard:
    """Satu salah ketik di URL tidak boleh bisa menyentuh cluster operator."""

    def test_accepts_test_cluster(self) -> None:
        require_test_cluster("postgresql://u:p@127.0.0.1:5435/payment_guard_test")

    def test_rejects_operator_port(self) -> None:
        with pytest.raises(EnvironmentError_, match="5434"):
            require_test_cluster("postgresql://u:p@127.0.0.1:5434/payment_guard_test")

    def test_rejects_operator_port_even_with_test_dbname(self) -> None:
        """Port diperiksa lebih dulu daripada nama database.

        Sebuah database bernama ``..._test`` **di cluster operator** tetap di
        cluster operator. Nama yang terlihat aman tidak membuat portnya aman.
        """
        with pytest.raises(EnvironmentError_, match="5434"):
            require_test_cluster("postgresql://u:p@127.0.0.1:5434/apa_pun_test")

    def test_rejects_remote_host(self) -> None:
        with pytest.raises(EnvironmentError_, match="loopback"):
            require_test_cluster("postgresql://u:p@10.0.0.5:5435/payment_guard_test")

    @pytest.mark.parametrize("dbname", ["payment_guard", "prod", "test_depan", "postgres"])
    def test_rejects_non_test_dbname(self, dbname: str) -> None:
        with pytest.raises(EnvironmentError_, match="_test"):
            require_test_cluster(f"postgresql://u:p@127.0.0.1:5435/{dbname}")

    def test_error_message_has_no_password(self) -> None:
        """Pesan penolakan tidak boleh memuat password dari URL."""
        with pytest.raises(EnvironmentError_) as info:
            require_test_cluster("postgresql://u:rahasia-sekali@127.0.0.1:5435/payment_guard")
        assert "rahasia-sekali" not in str(info.value)
