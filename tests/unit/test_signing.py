"""Envelope bertanda tangan: ST-03, ST-05, ST-06, dan tes signing PRD v2 §13.

Semua tes di sini murni: tanpa database, tanpa jaringan, tanpa berkas.

Seed uji di bawah **bukan kunci sungguhan** dan tidak pernah dipakai di luar
berkas ini. Ia ada supaya tanda tangan bisa dipaku sebagai literal: Ed25519
deterministik (RFC 8032), jadi seed tetap + pesan tetap selalu memberi tanda
tangan yang sama. Tanpa itu, tes hanya bisa membuktikan "tanda tangan yang
saya buat bisa saya verifikasi sendiri", yang akan tetap lulus walau awalan
domain atau bentuk kanoniknya berubah.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from payment_guard.canonical import canonical_bytes
from payment_guard.signing import (
    ED25519_SIGNATURE_SIZE,
    ENVELOPE_VERSION,
    PAYLOAD_TYPES,
    SIGNED_STATUS,
    SIGNING_DOMAIN_PREFIX,
    Envelope,
    EnvelopeError,
    Payload,
    PublicKeyEntry,
    SigningError,
    VerificationError,
    build_payload,
    check_not_expired,
    decode_public_key,
    dump_envelope,
    encode_public_key,
    parse_envelope,
    parse_public_keys,
    resolve_key,
    sign,
    signing_bytes,
    verify,
)

#: Seed uji. BUKAN kunci sungguhan: 32 byte berurutan 0x00..0x1f, dipilih
#: justru karena entropinya nol sehingga tidak bisa disalahartikan sebagai
#: bahan kunci asli. Hanya hidup di berkas tes ini.
SEED_UJI_JANGAN_DIPAKAI = bytes(range(32))

#: Seed uji kedua, untuk menguji tanda tangan dari kunci yang salah.
SEED_UJI_LAIN_JANGAN_DIPAKAI = bytes(range(32, 64))

KEY_ID = "guard-2026-11-a1b2"
KEY_ID_LAIN = "guard-2026-11-c3d4"

MANDATE_HASH = "1e940eab68a47c319a250eeafadbdb7c0917c414744996a4a3254f79e47377ec"
DECISION_ID = "0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f"
RECIPIENT = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"

ISSUED_AT = 1761820000
VALID_UNTIL = 1761820900


def guard_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED_UJI_JANGAN_DIPAKAI)


def other_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED_UJI_LAIN_JANGAN_DIPAKAI)


def payload(**overrides: Any) -> Payload:
    fields: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "decision_id": DECISION_ID,
        "mandate_id": "m-001",
        "agent_id": "demo-agent",
        "chain_id": 84532,
        "recipient": RECIPIENT,
        "amount_base": "25000000",
        "status": SIGNED_STATUS,
        "issued_at": ISSUED_AT,
        "valid_until": VALID_UNTIL,
        "mandate_hash": MANDATE_HASH,
    }
    fields.update(overrides)
    return Payload(**fields)


def registry(
    key_id: str = KEY_ID,
    pair: Ed25519PrivateKey | None = None,
    revoked: bool = False,
) -> dict[str, PublicKeyEntry]:
    material = encode_public_key((pair or guard_key()).public_key())
    return {key_id: PublicKeyEntry(key_id=key_id, public_key=material, revoked=revoked)}


def signed() -> Envelope:
    return sign(payload(), guard_key(), KEY_ID)


def envelope_json(**payload_overrides: Any) -> str:
    """Envelope JSON yang bisa diedit bebas, termasuk menjadi tidak sah."""
    envelope = signed()
    document = json.loads(dump_envelope(envelope))
    document["payload"].update(payload_overrides)
    return json.dumps(document)


# --------------------------------------------------------------------------- #
# Golden signature
# --------------------------------------------------------------------------- #


class TestGoldenSignature:
    """Tanda tangan dipaku sebagai literal, bukan dihitung ulang.

    Kalau tes ini gagal, salah satu dari ini berubah: awalan domain, bentuk
    kanonik, daftar field payload, atau nilai payload uji. Ketiga yang pertama
    adalah perubahan yang memutus kompatibilitas dengan envelope yang sudah
    beredar, dan itu keputusan operator.
    """

    #: Dipaku. Base64 standar, tanpa newline.
    GOLDEN_SIGNATURE = (
        "YV0pKhGwc7mcjf1WSrclFkveoZhhyK9PyMKMHkJg+Zp1s8FsmgaCvVg7i3XB/+JVORjwZ93Wi599oQGgOMIZDg=="
    )

    #: Byte kanonik payload uji, dipaku juga, supaya kegagalan menunjukkan APA
    #: yang berubah.
    GOLDEN_CANONICAL = (
        '{"agent_id":"demo-agent","amount_base":"25000000","chain_id":84532,'
        '"decision_id":"0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e1f",'
        '"issued_at":1761820000,'
        '"mandate_hash":"1e940eab68a47c319a250eeafadbdb7c0917c414744996a4a3254f79e47377ec",'
        '"mandate_id":"m-001","recipient":"0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",'
        '"status":"approved","v":"payment-guard/v1","valid_until":1761820900}'
    )

    def test_signature_matches_the_pinned_value(self) -> None:
        assert sign(payload(), guard_key(), KEY_ID).signature == self.GOLDEN_SIGNATURE

    def test_canonical_bytes_match_the_pinned_value(self) -> None:
        assert canonical_bytes(payload().as_dict()).decode("utf-8") == self.GOLDEN_CANONICAL

    def test_signing_bytes_are_prefix_plus_canonical(self) -> None:
        assert signing_bytes(payload()) == (
            b"payment-guard/v1\n" + self.GOLDEN_CANONICAL.encode("utf-8")
        )

    def test_signature_is_deterministic(self) -> None:
        """Ed25519 deterministik, jadi memaku tanda tangan itu sah."""
        signatures = {sign(payload(), guard_key(), KEY_ID).signature for _ in range(10)}
        assert signatures == {self.GOLDEN_SIGNATURE}

    def test_pinned_signature_decodes_to_64_bytes(self) -> None:
        assert len(base64.b64decode(self.GOLDEN_SIGNATURE, validate=True)) == (
            ED25519_SIGNATURE_SIZE
        )

    def test_signature_has_no_newline(self) -> None:
        assert "\n" not in self.GOLDEN_SIGNATURE
        assert self.GOLDEN_SIGNATURE.strip() == self.GOLDEN_SIGNATURE

    def test_pinned_signature_verifies(self) -> None:
        assert verify(signed(), registry()).decision_id == DECISION_ID


# --------------------------------------------------------------------------- #
# ST-03: satu field diubah -> verifikasi gagal
# --------------------------------------------------------------------------- #


class TestTamperedFields:
    """ST-03 dan Security v1.1 S3: tanda tangan menutupi semua field."""

    def test_untampered_envelope_verifies(self) -> None:
        """Penjaga: kalau ini gagal, tes di bawah tidak membuktikan apa pun."""
        assert verify(signed(), registry()).amount_base == "25000000"

    @pytest.mark.parametrize(
        ("field", "tampered"),
        [
            ("amount_base", "25000001"),
            ("amount_base", "250000000"),
            ("recipient", "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"),
            ("decision_id", "0191f3c7-1b2a-7c4d-8e5f-6a7b8c9d0e20"),
            ("mandate_hash", "0" * 64),
            ("valid_until", 1761899999),
            ("issued_at", 1761820001),
            ("mandate_id", "m-002"),
            ("agent_id", "agen-lain"),
        ],
    )
    def test_one_changed_field_fails_verification(self, field: str, tampered: object) -> None:
        envelope = signed()
        altered = Envelope(
            payload=payload(**{field: tampered}),
            key_id=envelope.key_id,
            signature=envelope.signature,
        )
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(altered, registry())

    @pytest.mark.parametrize(
        ("field", "tampered"),
        [
            ("amount_base", "25000001"),
            ("recipient", "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"),
            ("mandate_hash", "0" * 64),
        ],
    )
    def test_tampering_through_the_file_also_fails(self, field: str, tampered: object) -> None:
        """Jalur yang sesungguhnya: berkas diedit, lalu di-parse ulang."""
        envelope = parse_envelope(envelope_json(**{field: tampered}))
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(envelope, registry())

    def test_every_payload_field_is_covered_by_the_signature(self) -> None:
        """Dua arah: tidak ada field yang bisa diubah tanpa merusak tanda tangan.

        Field ``v``, ``chain_id``, dan ``status`` punya nilai terbatas, jadi
        perubahannya tertangkap pemeriksaan nilai; sembilan sisanya diuji di
        atas. Tes ini memastikan daftarnya tidak pernah tertinggal saat ada
        field baru.
        """
        value_constrained = {"v", "chain_id", "status"}
        tampered_in_tests = {
            "amount_base",
            "recipient",
            "decision_id",
            "mandate_hash",
            "valid_until",
            "issued_at",
            "mandate_id",
            "agent_id",
        }
        assert value_constrained | tampered_in_tests == set(PAYLOAD_TYPES)

    def test_signature_from_a_different_payload_fails(self) -> None:
        """Tanda tangan sah untuk payload lain tidak bisa dipindahkan."""
        other = sign(payload(amount_base="1"), guard_key(), KEY_ID)
        swapped = Envelope(payload=payload(), key_id=KEY_ID, signature=other.signature)
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(swapped, registry())


# --------------------------------------------------------------------------- #
# ST-05: parser ketat
# --------------------------------------------------------------------------- #


class TestStrictParsing:
    """ST-05 dan Security v1.1 E4."""

    def test_round_trip(self) -> None:
        envelope = signed()
        assert parse_envelope(dump_envelope(envelope)) == envelope

    def test_accepts_bytes_and_str(self) -> None:
        raw = dump_envelope(signed())
        assert parse_envelope(raw) == parse_envelope(raw.decode("utf-8"))

    def test_duplicate_payload_key_is_rejected(self) -> None:
        """json.loads biasanya menyimpan yang terakhir tanpa suara."""
        raw = (
            '{"payload": {"v": "payment-guard/v1", "v": "lain", '
            '"decision_id": "' + DECISION_ID + '", "mandate_id": "m-001", '
            '"agent_id": "demo-agent", "chain_id": 84532, '
            '"recipient": "' + RECIPIENT + '", "amount_base": "25000000", '
            '"status": "approved", "issued_at": 1761820000, '
            '"valid_until": 1761820900, "mandate_hash": "' + MANDATE_HASH + '"}, '
            '"key_id": "' + KEY_ID + '", "signature": "AA=="}'
        )
        with pytest.raises(EnvelopeError, match="kunci ganda"):
            parse_envelope(raw)

    def test_duplicate_top_level_key_is_rejected(self) -> None:
        raw = dump_envelope(signed()).decode("utf-8")
        injected = raw[:-1] + ',"key_id":"lain"}'
        with pytest.raises(EnvelopeError, match="kunci ganda"):
            parse_envelope(injected)

    def test_extra_payload_field_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"]["catatan"] = "apa saja"
        with pytest.raises(EnvelopeError, match="field tidak dikenal: catatan"):
            parse_envelope(json.dumps(document))

    def test_extra_top_level_field_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["alg"] = "ed25519"
        with pytest.raises(EnvelopeError, match="field tidak dikenal: alg"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("field", sorted(PAYLOAD_TYPES))
    def test_missing_payload_field_is_rejected(self, field: str) -> None:
        document = json.loads(dump_envelope(signed()))
        del document["payload"][field]
        with pytest.raises(EnvelopeError, match=f"field wajib tidak ada: {field}"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("field", ["payload", "key_id", "signature"])
    def test_missing_top_level_field_is_rejected(self, field: str) -> None:
        document = json.loads(dump_envelope(signed()))
        del document[field]
        with pytest.raises(EnvelopeError, match="field wajib tidak ada"):
            parse_envelope(json.dumps(document))

    def test_chain_id_as_string_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"]["chain_id"] = "84532"
        with pytest.raises(EnvelopeError, match="chain_id: harus int"):
            parse_envelope(json.dumps(document))

    def test_amount_base_as_int_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"]["amount_base"] = 25000000
        with pytest.raises(EnvelopeError, match="amount_base: harus str"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("field", ["chain_id", "issued_at", "valid_until"])
    def test_float_in_an_int_field_is_rejected(self, field: str) -> None:
        raw = dump_envelope(signed()).decode("utf-8")
        altered = raw.replace(f'"{field}":', f'"{field}":1.5,"_abaikan":')
        with pytest.raises(EnvelopeError, match="angka non-integer"):
            parse_envelope(altered)

    def test_float_anywhere_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        raw = json.dumps(document).replace('"issued_at": 1761820000', '"issued_at": 1761820000.0')
        with pytest.raises(EnvelopeError, match="angka non-integer"):
            parse_envelope(raw)

    def test_exponent_notation_is_rejected(self) -> None:
        document = json.loads(dump_envelope(signed()))
        raw = json.dumps(document).replace('"chain_id": 84532', '"chain_id": 8.4532e4')
        with pytest.raises(EnvelopeError, match="angka non-integer"):
            parse_envelope(raw)

    @pytest.mark.parametrize("field", ["chain_id", "issued_at", "valid_until"])
    def test_bool_in_an_int_field_is_rejected(self, field: str) -> None:
        """bool adalah subclass int di Python, jadi mudah lolos tanpa penjaga."""
        document = json.loads(dump_envelope(signed()))
        document["payload"][field] = True
        with pytest.raises(EnvelopeError, match="bool tidak diizinkan"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("field", ["v", "recipient", "amount_base", "status"])
    def test_bool_in_a_string_field_is_rejected(self, field: str) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"][field] = False
        with pytest.raises(EnvelopeError, match="bool tidak diizinkan"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("value", [None, [], {}, "teks"])
    def test_payload_must_be_an_object(self, value: object) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"] = value
        with pytest.raises(EnvelopeError):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("raw", ["", "{", "null", "[]", '"teks"', "123"])
    def test_malformed_json_is_rejected(self, raw: str) -> None:
        with pytest.raises(EnvelopeError):
            parse_envelope(raw)

    def test_non_utf8_bytes_are_rejected(self) -> None:
        with pytest.raises(EnvelopeError):
            parse_envelope(b"\xff\xfe{}")


class TestKeyOrderAndWhitespaceDoNotMatter:
    """ST-05: urutan kunci acak diterima, dan hasilnya sama."""

    def test_shuffled_key_order_gives_the_same_signature_check(self) -> None:
        document = json.loads(dump_envelope(signed()))
        shuffled_payload = dict(reversed(list(document["payload"].items())))
        shuffled = {
            "signature": document["signature"],
            "key_id": document["key_id"],
            "payload": shuffled_payload,
        }
        envelope = parse_envelope(json.dumps(shuffled))
        assert verify(envelope, registry()).decision_id == DECISION_ID
        assert envelope == signed()

    def test_indentation_and_spacing_do_not_matter(self) -> None:
        document = json.loads(dump_envelope(signed()))
        pretty = json.dumps(document, indent=4, sort_keys=False)
        assert verify(parse_envelope(pretty), registry()).decision_id == DECISION_ID

    def test_verification_uses_the_parsed_payload_not_raw_bytes(self) -> None:
        """Byte file berbeda, hasil verifikasi sama.

        Ini yang membuat spasi dan urutan kunci tidak berpengaruh: yang
        di-hash selalu bentuk kanonik dari nilai hasil parse.
        """
        compact = dump_envelope(signed())
        pretty = json.dumps(json.loads(compact), indent=2).encode("utf-8")
        assert compact != pretty
        assert parse_envelope(compact) == parse_envelope(pretty)

    def test_trailing_newline_in_the_file_is_tolerated(self) -> None:
        raw = dump_envelope(signed()) + b"\n"
        assert verify(parse_envelope(raw), registry()).decision_id == DECISION_ID


class TestValueShapes:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("recipient", "0x5AAEB6053F3E94C9B9A09F33669435E7EF1BEAED"),
            ("recipient", "0x123"),
            ("recipient", "5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"),
            ("mandate_hash", "ABCDEF" + "0" * 58),
            ("mandate_hash", "0" * 63),
            ("decision_id", "not-a-uuid"),
            ("decision_id", DECISION_ID.upper()),
            ("amount_base", "025000000"),
            ("amount_base", "25.000000"),
            ("amount_base", "-1"),
            ("amount_base", "0"),
            ("amount_base", ""),
            ("mandate_id", ""),
            ("agent_id", ""),
            ("agent_id", "x" * 65),
            ("issued_at", 0),
            ("issued_at", -1),
            ("valid_until", 0),
        ],
    )
    def test_bad_shape_is_rejected_at_parse(self, field: str, value: object) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"][field] = value
        with pytest.raises(EnvelopeError):
            parse_envelope(json.dumps(document))

    def test_valid_until_must_be_after_issued_at(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["payload"]["valid_until"] = document["payload"]["issued_at"]
        with pytest.raises(EnvelopeError, match="valid_until"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize(
        "key_id", ["", "Guard-2026", "guard 2026", "-guard", "x" * 65, "guard/2026"]
    )
    def test_bad_key_id_is_rejected(self, key_id: str) -> None:
        document = json.loads(dump_envelope(signed()))
        document["key_id"] = key_id
        with pytest.raises(EnvelopeError, match="key_id tidak sah"):
            parse_envelope(json.dumps(document))

    @pytest.mark.parametrize("signature", ["", "bukan base64!", "AA", "QUJD\nQUJD"])
    def test_bad_signature_encoding_is_rejected(self, signature: str) -> None:
        document = json.loads(dump_envelope(signed()))
        document["signature"] = signature
        with pytest.raises(EnvelopeError):
            parse_envelope(json.dumps(document))

    def test_signature_of_wrong_length_fails_at_verify(self) -> None:
        document = json.loads(dump_envelope(signed()))
        document["signature"] = base64.b64encode(b"\x00" * 32).decode("ascii")
        with pytest.raises(VerificationError, match="64 byte"):
            verify(parse_envelope(json.dumps(document)), registry())


# --------------------------------------------------------------------------- #
# ST-06: kunci dan awalan domain
# --------------------------------------------------------------------------- #


class TestKeyResolution:
    """ST-06 dan Security v1.1 E5."""

    def test_unknown_key_id_fails(self) -> None:
        with pytest.raises(VerificationError, match="key_id tidak dikenal"):
            verify(signed(), registry(key_id=KEY_ID_LAIN))

    def test_revoked_key_fails(self) -> None:
        with pytest.raises(VerificationError, match="sudah dicabut"):
            verify(signed(), registry(revoked=True))

    def test_revoked_key_fails_even_though_the_signature_is_valid(self) -> None:
        """Pencabutan diperiksa sebelum tanda tangan, dan itu memang urutannya."""
        envelope = signed()
        assert verify(envelope, registry()).decision_id == DECISION_ID
        with pytest.raises(VerificationError, match="dicabut"):
            verify(envelope, registry(revoked=True))

    def test_signature_from_another_key_fails(self) -> None:
        envelope = sign(payload(), other_key(), KEY_ID)
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(envelope, registry())

    def test_empty_registry_fails(self) -> None:
        with pytest.raises(VerificationError, match="key_id tidak dikenal"):
            verify(signed(), {})

    def test_resolve_key_returns_the_public_key(self) -> None:
        key = resolve_key(registry(), KEY_ID)
        assert encode_public_key(key) == encode_public_key(guard_key().public_key())

    def test_error_messages_do_not_contain_key_material(self) -> None:
        material = encode_public_key(guard_key().public_key())
        for builder in (
            lambda: verify(signed(), registry(key_id=KEY_ID_LAIN)),
            lambda: verify(signed(), registry(revoked=True)),
            lambda: verify(sign(payload(), other_key(), KEY_ID), registry()),
        ):
            with pytest.raises(VerificationError) as exc:
                builder()
            message = str(exc.value)
            assert material not in message
            assert SEED_UJI_JANGAN_DIPAKAI.hex() not in message


class TestDomainPrefix:
    """ST-06: tanda tangan tanpa awalan domain harus gagal."""

    def test_prefix_is_the_documented_one(self) -> None:
        assert SIGNING_DOMAIN_PREFIX == b"payment-guard/v1\n"

    def test_signature_without_the_prefix_fails(self) -> None:
        """Tanda tangan dibuat atas byte kanonik saja, lalu diverifikasi utuh."""
        without_prefix = guard_key().sign(canonical_bytes(payload().as_dict()))
        envelope = Envelope(
            payload=payload(),
            key_id=KEY_ID,
            signature=base64.b64encode(without_prefix).decode("ascii"),
        )
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(envelope, registry())

    def test_signature_with_a_different_prefix_fails(self) -> None:
        wrong = guard_key().sign(
            b"payment-guard/mandate/v1\n" + canonical_bytes(payload().as_dict())
        )
        envelope = Envelope(
            payload=payload(),
            key_id=KEY_ID,
            signature=base64.b64encode(wrong).decode("ascii"),
        )
        with pytest.raises(VerificationError, match="tanda tangan tidak cocok"):
            verify(envelope, registry())

    def test_prefix_is_not_part_of_the_canonical_bytes(self) -> None:
        assert not canonical_bytes(payload().as_dict()).startswith(SIGNING_DOMAIN_PREFIX)


# --------------------------------------------------------------------------- #
# Hanya approved yang ditandatangani
# --------------------------------------------------------------------------- #


class TestOnlyApprovedIsSigned:
    @pytest.mark.parametrize(
        "status", ["denied", "needs_human", "rejected_by_human", "expired", "", "Approved"]
    )
    def test_other_statuses_cannot_be_signed(self, status: str) -> None:
        with pytest.raises(SigningError, match="hanya status 'approved'"):
            sign(payload(status=status), guard_key(), KEY_ID)

    @pytest.mark.parametrize("status", ["denied", "needs_human", "expired"])
    def test_build_payload_refuses_other_statuses(self, status: str) -> None:
        with pytest.raises(SigningError, match="hanya status 'approved'"):
            build_payload(
                decision_id=DECISION_ID,
                mandate_id="m-001",
                agent_id="demo-agent",
                recipient=RECIPIENT,
                amount_base=25_000_000,
                issued_at=ISSUED_AT,
                valid_until=VALID_UNTIL,
                mandate_hash=MANDATE_HASH,
                status=status,
            )

    def test_a_non_approved_status_fails_value_check_at_verify(self) -> None:
        """Kalau envelope semacam itu entah bagaimana ada, verify menolaknya."""
        altered = payload(status="denied")
        signature = guard_key().sign(signing_bytes(altered))
        envelope = Envelope(
            payload=altered,
            key_id=KEY_ID,
            signature=base64.b64encode(signature).decode("ascii"),
        )
        with pytest.raises(VerificationError, match="bukan 'approved'"):
            verify(envelope, registry())


class TestValueChecksHappenAfterSignature:
    """PRD v2.1 §9: alasan penolakan cek 2 berbeda dari cek 3.

    Parser hanya menegakkan bentuk, jadi ``v`` dan ``status`` yang salah lolos
    parse dan ditolak :func:`verify`. Kalau parser ikut menolaknya, Executor
    akan melaporkan ``signature_invalid`` padahal masalahnya versi atau status.
    """

    def test_wrong_version_passes_parse_but_fails_verify(self) -> None:
        altered = payload(v="payment-guard/v2")
        signature = guard_key().sign(signing_bytes(altered))
        document = {
            "payload": altered.as_dict(),
            "key_id": KEY_ID,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        envelope = parse_envelope(json.dumps(document))
        with pytest.raises(VerificationError, match="versi envelope"):
            verify(envelope, registry())

    def test_wrong_chain_id_passes_parse_but_fails_verify(self) -> None:
        altered = payload(chain_id=1)
        signature = guard_key().sign(signing_bytes(altered))
        document = {
            "payload": altered.as_dict(),
            "key_id": KEY_ID,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        envelope = parse_envelope(json.dumps(document))
        with pytest.raises(VerificationError, match="chain_id 1 bukan 84532"):
            verify(envelope, registry())


# --------------------------------------------------------------------------- #
# build_payload
# --------------------------------------------------------------------------- #


class TestBuildPayload:
    def build(self, **overrides: Any) -> Payload:
        fields: dict[str, Any] = {
            "decision_id": DECISION_ID,
            "mandate_id": "m-001",
            "agent_id": "demo-agent",
            "recipient": RECIPIENT,
            "amount_base": 25_000_000,
            "issued_at": ISSUED_AT,
            "valid_until": VALID_UNTIL,
            "mandate_hash": MANDATE_HASH,
        }
        fields.update(overrides)
        return build_payload(**fields)

    def test_fills_v_and_status(self) -> None:
        built = self.build()
        assert built.v == ENVELOPE_VERSION
        assert built.status == SIGNED_STATUS

    def test_chain_id_comes_from_the_chain_registry(self) -> None:
        assert self.build().chain_id == 84532

    def test_unsupported_chain_is_rejected(self) -> None:
        from payment_guard.chains import ChainError

        with pytest.raises(ChainError):
            self.build(chain="ethereum")

    def test_amount_base_becomes_a_string(self) -> None:
        built = self.build(amount_base=1)
        assert built.amount_base == "1"
        assert isinstance(built.amount_base, str)

    def test_large_amount_keeps_every_digit(self) -> None:
        huge = 9_007_199_254_740_993_000_001
        assert self.build(amount_base=huge).amount_base == str(huge)

    @pytest.mark.parametrize("amount", [0, -1])
    def test_non_positive_amount_rejected(self, amount: int) -> None:
        with pytest.raises((SigningError, ValueError)):
            self.build(amount_base=amount)

    def test_matches_the_golden_payload(self) -> None:
        assert self.build() == payload()


# --------------------------------------------------------------------------- #
# Registri kunci publik
# --------------------------------------------------------------------------- #


class TestParsePublicKeys:
    def valid_document(self, **overrides: Any) -> str:
        entry = {
            "key_id": KEY_ID,
            "public_key": encode_public_key(guard_key().public_key()),
            "revoked": False,
        }
        entry.update(overrides)
        return json.dumps({"keys": [entry]})

    def test_round_trip(self) -> None:
        parsed = parse_public_keys(self.valid_document())
        assert set(parsed) == {KEY_ID}
        assert parsed[KEY_ID].revoked is False

    def test_revoked_true_is_kept(self) -> None:
        parsed = parse_public_keys(self.valid_document(revoked=True))
        assert parsed[KEY_ID].revoked is True

    def test_duplicate_key_id_rejected(self) -> None:
        entry = {
            "key_id": KEY_ID,
            "public_key": encode_public_key(guard_key().public_key()),
            "revoked": False,
        }
        with pytest.raises(EnvelopeError, match="key_id ganda"):
            parse_public_keys(json.dumps({"keys": [entry, dict(entry)]}))

    def test_duplicate_json_key_rejected(self) -> None:
        with pytest.raises(EnvelopeError, match="kunci ganda"):
            parse_public_keys('{"keys": [], "keys": []}')

    @pytest.mark.parametrize(
        "document",
        [
            "{}",
            '{"keys": []}',
            '{"keys": {}}',
            '{"keys": [], "lain": 1}',
            '{"kunci": []}',
            "[]",
            "null",
        ],
    )
    def test_bad_shape_rejected(self, document: str) -> None:
        with pytest.raises(EnvelopeError):
            parse_public_keys(document)

    def test_entry_with_extra_field_rejected(self) -> None:
        with pytest.raises(EnvelopeError, match="tepat"):
            parse_public_keys(self.valid_document(catatan="x"))

    @pytest.mark.parametrize("revoked", ["false", 0, None])
    def test_revoked_must_be_boolean(self, revoked: object) -> None:
        with pytest.raises(EnvelopeError, match="revoked harus true atau false"):
            parse_public_keys(self.valid_document(revoked=revoked))

    @pytest.mark.parametrize("material", ["", "bukan base64!", "AAAA", "QUJD"])
    def test_bad_public_key_rejected(self, material: str) -> None:
        with pytest.raises(EnvelopeError):
            parse_public_keys(self.valid_document(public_key=material))

    def test_public_key_of_wrong_length_rejected(self) -> None:
        short = base64.b64encode(b"\x00" * 31).decode("ascii")
        with pytest.raises(EnvelopeError, match="32 byte"):
            parse_public_keys(self.valid_document(public_key=short))

    def test_registry_never_contains_secret_material(self) -> None:
        """Berkas publik hanya memuat public key, 32 byte."""
        document = self.valid_document()
        assert SEED_UJI_JANGAN_DIPAKAI.hex() not in document
        assert base64.b64encode(SEED_UJI_JANGAN_DIPAKAI).decode("ascii") not in document


class TestPublicKeyEncoding:
    def test_round_trip(self) -> None:
        material = encode_public_key(guard_key().public_key())
        assert encode_public_key(decode_public_key(material)) == material

    def test_encoding_is_32_bytes_base64(self) -> None:
        material = encode_public_key(guard_key().public_key())
        assert len(base64.b64decode(material, validate=True)) == 32
        assert "\n" not in material


# --------------------------------------------------------------------------- #
# Kedaluwarsa (jam disuntikkan)
# --------------------------------------------------------------------------- #


class TestExpiry:
    """PRD v2 §13 menyebut "kedaluwarsa -> gagal" di antara tes signing.

    Jam selalu disuntikkan: ``guard verify`` tidak memanggil fungsi ini karena
    ia bekerja tanpa database, dan jam yang dipercaya adalah jam database.
    """

    def test_before_valid_until_passes(self) -> None:
        assert check_not_expired(payload(), VALID_UNTIL - 1) is None

    def test_exactly_at_valid_until_passes(self) -> None:
        """Batas inklusif di sisi boleh, seperti batas lain di sistem ini."""
        assert check_not_expired(payload(), VALID_UNTIL) is None

    def test_one_second_after_fails(self) -> None:
        with pytest.raises(VerificationError, match="kedaluwarsa"):
            check_not_expired(payload(), VALID_UNTIL + 1)

    @pytest.mark.parametrize("now", [0, -1])
    def test_non_positive_now_rejected(self, now: int) -> None:
        with pytest.raises(VerificationError, match="now"):
            check_not_expired(payload(), now)

    @pytest.mark.parametrize("now", [1.0, "123", None, True])
    def test_non_int_now_rejected(self, now: object) -> None:
        with pytest.raises(VerificationError, match="now"):
            check_not_expired(payload(), now)  # type: ignore[arg-type]

    def test_verify_does_not_check_expiry(self) -> None:
        """Tanpa jam, verify tidak bisa dan tidak boleh memutuskan kedaluwarsa."""
        assert verify(signed(), registry()).valid_until == VALID_UNTIL


# --------------------------------------------------------------------------- #
# Bentuk dan tipe
# --------------------------------------------------------------------------- #


class TestShape:
    def test_payload_has_exactly_eleven_fields(self) -> None:
        assert len(PAYLOAD_TYPES) == 11
        assert set(Payload.__dataclass_fields__) == set(PAYLOAD_TYPES)

    def test_payload_field_set_matches_prd_v2_plus_mandate_hash(self) -> None:
        """PRD v2 §7 sepuluh field; PRD v2.1 §7 menambahkan mandate_hash."""
        prd_v2 = {
            "v",
            "decision_id",
            "mandate_id",
            "agent_id",
            "chain_id",
            "recipient",
            "amount_base",
            "status",
            "issued_at",
            "valid_until",
        }
        assert set(PAYLOAD_TYPES) == prd_v2 | {"mandate_hash"}

    def test_envelope_has_exactly_three_fields(self) -> None:
        assert set(Envelope.__dataclass_fields__) == {"payload", "key_id", "signature"}

    def test_no_alg_field(self) -> None:
        """Security v1.1 E5: tanpa field algoritma yang bisa dipilih penyerang."""
        assert "alg" not in PAYLOAD_TYPES
        assert "alg" not in Envelope.__dataclass_fields__

    def test_payload_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            payload().v = "lain"  # type: ignore[misc]

    def test_envelope_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            signed().key_id = "lain"  # type: ignore[misc]

    def test_as_dict_key_order_does_not_affect_canonical_bytes(self) -> None:
        reordered = {k: payload().as_dict()[k] for k in reversed(list(PAYLOAD_TYPES))}
        assert canonical_bytes(reordered) == canonical_bytes(payload().as_dict())

    def test_sign_rejects_a_non_payload(self) -> None:
        with pytest.raises(SigningError, match="payload harus Payload"):
            sign({"v": ENVELOPE_VERSION}, guard_key(), KEY_ID)  # type: ignore[arg-type]

    def test_sign_rejects_a_bad_key_id(self) -> None:
        with pytest.raises(SigningError, match="key_id tidak sah"):
            sign(payload(), guard_key(), "Guard 2026")

    def test_sign_rejects_a_non_ed25519_key(self) -> None:
        with pytest.raises(SigningError, match="Ed25519"):
            sign(payload(), object(), KEY_ID)  # type: ignore[arg-type]

    def test_verify_rejects_a_non_envelope(self) -> None:
        with pytest.raises(VerificationError, match="envelope harus Envelope"):
            verify({"payload": {}}, registry())  # type: ignore[arg-type]
