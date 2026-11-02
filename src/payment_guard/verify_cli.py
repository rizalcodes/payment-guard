"""``guard verify``: periksa envelope hanya dengan kunci publik.

PRD v2 bagian 4 ("Verifier") dan bagian 7. Modul ini mensimulasikan pihak
penerima: ia membuktikan bahwa Guard benar-benar menyetujui sebuah pembayaran,
**tanpa** database, tanpa jaringan, dan tanpa chain.

Modul ini hanya membaca dua berkas: envelope, dan registri kunci publik. Ia
tidak punya jalur untuk membaca bahan kunci rahasia, dan tidak mengimpor apa
pun yang menyimpan lokasinya (Arsitektur v1.1 bagian 2, Security v1.1 E2).
Ada tes yang memeriksa sumber modul ini untuk memastikan itu tetap benar.

Urutannya mengikuti PRD v2.1 bagian 9:

1. parse ketat
2. cari ``key_id`` di registri; tolak bila tidak dikenal atau sudah dicabut
3. verifikasi tanda tangan
4. cek ``v``, ``chain_id``, dan ``status``
5. opsional: cocokkan ``mandate_hash`` dengan berkas mandate

Langkah 5 menjawab pertanyaan yang tidak bisa dijawab tanda tangan: envelope
ini sah, tapi apakah ia berasal dari mandate yang **kamu** maksud? Tanpa itu
operator bisa yakin pada envelope yang ditandatangani dengan benar atas aturan
yang salah.

Kedaluwarsa **tidak** diperiksa di sini. Satu-satunya jam yang dipercaya
adalah jam database (Arsitektur v1.1 prinsip 5), dan perintah ini bekerja
tanpa database. Executor memeriksanya di cek 3b.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from payment_guard.mandate import MandateError, compute_mandate_hash, load_mandate
from payment_guard.signing import (
    EnvelopeError,
    Payload,
    VerificationError,
    parse_envelope,
    parse_public_keys,
    verify,
)

#: Kata yang dicetak bila semua pemeriksaan lolos.
VALID_LABEL = "VALID"

#: Awalan baris bila ada pemeriksaan yang gagal.
INVALID_LABEL = "INVALID"


@dataclass(frozen=True)
class VerifyOutcome:
    """Hasil satu pemeriksaan.

    ``reason`` hanya terisi bila gagal, dan isinya ditujukan untuk operator:
    ia tidak pernah memuat bahan kunci.
    """

    valid: bool
    reason: str | None = None
    payload: Payload | None = None

    def render(self) -> str:
        """Satu baris keluaran: ``VALID`` atau ``INVALID: <alasan>``."""
        if self.valid:
            return VALID_LABEL
        return f"{INVALID_LABEL}: {self.reason}"


def verify_envelope_file(
    envelope_path: Path,
    public_keys_path: Path,
    mandate_path: Path | None = None,
) -> VerifyOutcome:
    """Periksa envelope di ``envelope_path`` terhadap registri kunci publik.

    Tidak pernah melempar untuk kegagalan verifikasi: kegagalan dikembalikan
    sebagai :class:`VerifyOutcome` supaya pemanggil bisa mencetaknya dalam
    satu bentuk. Berkas yang tidak terbaca juga menjadi hasil, bukan traceback.
    """
    envelope_raw = _read(envelope_path, "envelope")
    if isinstance(envelope_raw, VerifyOutcome):
        return envelope_raw

    registry_raw = _read(public_keys_path, "berkas kunci publik")
    if isinstance(registry_raw, VerifyOutcome):
        return registry_raw

    try:
        envelope = parse_envelope(envelope_raw)
    except EnvelopeError as exc:
        return VerifyOutcome(valid=False, reason=f"envelope tidak bisa di-parse: {exc}")

    try:
        registry = parse_public_keys(registry_raw)
    except EnvelopeError as exc:
        return VerifyOutcome(valid=False, reason=f"registri kunci publik tidak sah: {exc}")

    try:
        payload = verify(envelope, registry)
    except (VerificationError, EnvelopeError) as exc:
        return VerifyOutcome(valid=False, reason=str(exc))

    if mandate_path is not None:
        mismatch = _check_mandate(payload, mandate_path)
        if mismatch is not None:
            return mismatch

    return VerifyOutcome(valid=True, payload=payload)


def _check_mandate(payload: Payload, mandate_path: Path) -> VerifyOutcome | None:
    """Bandingkan ``mandate_hash`` di payload dengan berkas mandate.

    Mengembalikan ``None`` bila cocok.
    """
    raw = _read(mandate_path, "berkas mandate")
    if isinstance(raw, VerifyOutcome):
        return raw

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        return VerifyOutcome(valid=False, reason=f"berkas mandate bukan JSON yang sah: {exc.msg}")

    try:
        expected = compute_mandate_hash(load_mandate(document))
    except (MandateError, ValueError) as exc:
        return VerifyOutcome(valid=False, reason=f"berkas mandate tidak sah: {exc}")

    if payload.mandate_hash != expected:
        return VerifyOutcome(
            valid=False,
            reason=(
                "mandate_hash tidak cocok dengan berkas mandate; envelope ini "
                "ditandatangani atas aturan yang lain.\n"
                f"  di envelope : {payload.mandate_hash}\n"
                f"  dari berkas : {expected}"
            ),
        )
    return None


def _read(path: Path, label: str) -> bytes | VerifyOutcome:
    """Baca berkas, atau kembalikan hasil gagal yang sudah berbentuk."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return VerifyOutcome(valid=False, reason=f"{label} tidak ada: {path}")
    except IsADirectoryError:
        return VerifyOutcome(valid=False, reason=f"{label} adalah direktori: {path}")
    except OSError as exc:
        return VerifyOutcome(valid=False, reason=f"{label} tidak bisa dibaca: {exc.strerror}")


def describe(payload: Payload) -> list[str]:
    """Baris rincian untuk operator setelah hasil VALID.

    Hanya field payload. Tidak ada bahan kunci di sini.
    """
    return [
        f"  decision_id  : {payload.decision_id}",
        f"  agent_id     : {payload.agent_id}",
        f"  mandate_id   : {payload.mandate_id}",
        f"  mandate_hash : {payload.mandate_hash}",
        f"  recipient    : {payload.recipient}",
        f"  amount_base  : {payload.amount_base}",
        f"  chain_id     : {payload.chain_id}",
        f"  issued_at    : {payload.issued_at}",
        f"  valid_until  : {payload.valid_until}",
    ]
