"""``guard keygen``: buat pasangan kunci Ed25519 untuk tanda tangan Guard.

Security v1.1 bagian 7 dan runbook 8.1. Dipisah dari
:mod:`payment_guard.signing` karena modul ini menulis berkas dan membaca jam,
sedangkan ``signing`` harus tetap murni.

Dua berkas ditulis, dan pembagiannya adalah pembagian kepercayaan:

``keys/guard_signing.json``
    Bahan kunci rahasia. Dibaca Guard dan Approvals CLI. Di-gitignore, dibuat
    dengan izin 0600 sejauh OS mendukung.

``guard_public.json``
    Hanya public key. Dibaca Executor dan ``guard verify``. **Boleh
    di-commit** (Arsitektur v1.1 bagian 2.1).

Aturan yang ditegakkan di sini:

* Bahan kunci rahasia **tidak pernah** dicetak. Keygen hanya melaporkan
  ``key_id`` dan lokasi berkas.
* Berkas rahasia yang sudah ada **tidak pernah** ditimpa. Penimpaan berarti
  kehilangan kunci yang mungkin masih dipakai envelope yang beredar, dan itu
  tidak bisa dibatalkan. Rotasi berjalan lewat ``key_id`` baru plus
  ``revoked: true`` pada entri lama (runbook 8.1).
* ``guard_public.json`` ditambahi, bukan ditimpa: entri lama tetap ada supaya
  envelope yang ditandatangani kunci lama masih bisa diverifikasi sampai
  operator mencabutnya dengan sengaja.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from payment_guard.signing import (
    KEY_ID_PATTERN,
    PublicKeyEntry,
    encode_public_key,
    parse_public_keys,
)

#: Lokasi bawaan, relatif terhadap akar repo.
SIGNING_KEY_RELATIVE_PATH: Final = "keys/guard_signing.json"
PUBLIC_KEYS_RELATIVE_PATH: Final = "guard_public.json"

#: Jumlah byte acak di akhir ``key_id``. Dua byte = empat karakter hex,
#: sesuai contoh ``guard-2026-11-a1b2`` di PRD v2 bagian 7.
_KEY_ID_RANDOM_BYTES: Final = 2

#: Izin berkas rahasia. Hanya berlaku di OS yang memetakan mode POSIX.
_SECRET_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR


class KeygenError(RuntimeError):
    """Keygen tidak bisa dilanjutkan. Pesannya tidak memuat bahan kunci."""


@dataclass(frozen=True)
class KeygenResult:
    """Ringkasan hasil keygen. Sengaja tidak memuat bahan kunci rahasia."""

    key_id: str
    signing_key_path: Path
    public_keys_path: Path
    public_keys_total: int

    def summary_lines(self) -> list[str]:
        """Baris laporan untuk operator. Tanpa bahan kunci apa pun."""
        return [
            f"key_id        : {self.key_id}",
            f"kunci rahasia : {self.signing_key_path}  (jangan di-commit)",
            f"kunci publik  : {self.public_keys_path}  ({self.public_keys_total} entri)",
            "",
            "Bahan kunci rahasia tidak ditampilkan dengan sengaja.",
            "Rotasi: jalankan keygen dengan berkas rahasia lama dipindahkan, lalu",
            "setel revoked: true pada entri lama (Security v1.1 runbook 8.1).",
        ]


def new_key_id(now: datetime | None = None) -> str:
    """Susun ``key_id`` baru, mis. ``guard-2026-11-a1b2``.

    Tahun dan bulan membuatnya mudah diurutkan dan dibaca manusia; empat
    karakter acak mencegah tabrakan bila dua kunci dibuat di bulan yang sama.
    """
    moment = now if now is not None else datetime.now(UTC)
    suffix = secrets.token_hex(_KEY_ID_RANDOM_BYTES)
    key_id = f"guard-{moment:%Y-%m}-{suffix}"
    if KEY_ID_PATTERN.match(key_id) is None:  # pragma: no cover - penjaga bentuk
        raise KeygenError(f"key_id yang dihasilkan tidak sah: {key_id!r}")
    return key_id


def run_keygen(
    signing_key_path: Path,
    public_keys_path: Path,
    *,
    now: datetime | None = None,
) -> KeygenResult:
    """Buat pasangan kunci dan tulis kedua berkas.

    Raises:
        KeygenError: bila ``signing_key_path`` sudah ada, atau bila
            ``public_keys_path`` ada tapi tidak bisa dibaca sebagai registri.
    """
    if signing_key_path.exists():
        raise KeygenError(
            f"{signing_key_path} sudah ada, dan keygen tidak pernah menimpanya.\n"
            "Menimpanya akan menghilangkan kunci yang mungkin masih dipakai envelope\n"
            "yang beredar, dan itu tidak bisa dibatalkan.\n"
            "Untuk rotasi: pindahkan berkas itu ke tempat aman, jalankan keygen lagi,\n"
            f"lalu setel revoked: true pada entri lama di {public_keys_path}\n"
            "(Security v1.1 runbook 8.1)."
        )

    # Registri publik dibaca lebih dulu: kalau berkasnya rusak, lebih baik
    # gagal sebelum ada kunci baru yang separuh tertulis.
    existing = _read_existing_registry(public_keys_path)

    key_id = new_key_id(now)
    if key_id in existing:  # pragma: no cover - 1 dari 65536 per bulan
        raise KeygenError(f"key_id {key_id} sudah ada di {public_keys_path}; coba lagi")

    key = Ed25519PrivateKey.generate()
    _write_signing_key(signing_key_path, key_id, key)

    entries = [
        *existing.values(),
        PublicKeyEntry(key_id, encode_public_key(key.public_key()), False),
    ]
    _write_public_keys(public_keys_path, entries)

    return KeygenResult(
        key_id=key_id,
        signing_key_path=signing_key_path,
        public_keys_path=public_keys_path,
        public_keys_total=len(entries),
    )


def _read_existing_registry(path: Path) -> dict[str, PublicKeyEntry]:
    if not path.exists():
        return {}
    try:
        return parse_public_keys(path.read_bytes())
    except ValueError as exc:
        raise KeygenError(
            f"{path} sudah ada tapi tidak bisa dibaca sebagai registri kunci: {exc}"
        ) from None


def _write_signing_key(path: Path, key_id: str, key: Ed25519PrivateKey) -> None:
    """Tulis bahan kunci rahasia dengan izin terbatas.

    ``O_CREAT | O_EXCL`` membuat "tolak bila sudah ada" bersifat atomik, jadi
    tidak ada jendela antara memeriksa dan menulis. Mode 0600 diberikan saat
    pembuatan, bukan sesudahnya, supaya berkasnya tidak pernah sempat ada
    dengan izin yang lebih longgar.
    """
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    document = {
        "key_id": key_id,
        "algorithm": "ed25519",
        "private_key": base64.b64encode(raw).decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SECRET_FILE_MODE)
    except FileExistsError:
        raise KeygenError(f"{path} sudah ada; keygen tidak menimpanya") from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        path.chmod(_SECRET_FILE_MODE)
    except OSError:  # pragma: no cover - Windows tidak memetakan mode POSIX
        pass


def _write_public_keys(path: Path, entries: list[PublicKeyEntry]) -> None:
    """Tulis registri publik. Tidak memuat bahan kunci rahasia."""
    document = {
        "keys": [
            {"key_id": e.key_id, "public_key": e.public_key, "revoked": e.revoked} for e in entries
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
