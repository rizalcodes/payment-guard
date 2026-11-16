"""Jaringan diteruskan ke jalur mandate dan signing, bukan dibaca diam-diam.

Lima hal yang dijaga berkas ini, dan kelimanya diminta pemilik secara
eksplisit:

a. mandate devnet **diterima** saat jaringan devnet
b. mandate devnet **ditolak** di jaringan produksi
c. mandate produksi **ditolak** di jaringan devnet
d. envelope devnet memuat ``chain_id`` 31337
e. verifikasi menolak envelope yang ``chain_id``-nya tidak cocok jaringannya

Ditambah penjaga regresi yang justru paling penting: **tanpa argumen jaringan,
semuanya berperilaku produksi**. Kalau penjaga itu memerah, perubahan devnet
telah menggeser perilaku produksi — dan itu kegagalan yang jauh lebih serius
daripada devnet yang tidak jalan.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from payment_guard.chains import (
    BASE_SEPOLIA,
    DEVNET_LOCAL,
    NETWORK_DEVNET,
    NETWORK_PRODUCTION,
    ChainError,
    chain_for_network,
    chain_id_for,
    is_supported_chain,
    supported_chains,
)
from payment_guard.mandate import MandateError, load_mandate
from payment_guard.signing import (
    PublicKeyEntry,
    SigningError,
    VerificationError,
    build_payload,
    encode_public_key,
    sign,
    verify,
)

#: Kunci tanda tangan uji, deterministik. Bukan rahasia, bukan kunci produksi.
#: Berpola supaya jelas buatan dan tidak memicu pemindai rahasia.
SEED_UJI = bytes(range(32))

#: ``key_id`` uji, bentuknya mengikuti yang diterima parser envelope.
KEY_ID = "guard-2026-11-dev1"


def guard_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED_UJI)


def registry() -> dict[str, PublicKeyEntry]:
    material = encode_public_key(guard_key().public_key())
    return {KEY_ID: PublicKeyEntry(key_id=KEY_ID, public_key=material, revoked=False)}


#: Mandate sintetis minimal. ``chain`` diisi pemanggil.
BASE_MANDATE: dict[str, Any] = {
    "mandate_id": "m-devnet-1",
    "agent_id": "devnet-agent",
    "chain": "",
    "allowed_recipients": ["0x" + "11" * 20],
    "max_per_tx": "25.000000",
    "max_per_day": "100.000000",
    "human_approval_above": "25.000000",
    "retry_cooldown_seconds": 60,
    "aggregation_window_seconds": 3600,
    "decision_ttl_seconds": 300,
    "human_wait_ttl_seconds": 600,
}


def mandate_doc(chain: str) -> dict[str, Any]:
    return {**BASE_MANDATE, "chain": chain}


class TestProductionBehaviourIsUnchanged:
    """Penjaga regresi. Ini yang harus hijau sebelum yang lain penting."""

    def test_chain_id_for_without_a_network_is_base_sepolia(self) -> None:
        assert chain_id_for(BASE_SEPOLIA) == 84532

    def test_is_supported_chain_without_a_network_is_production(self) -> None:
        assert is_supported_chain(BASE_SEPOLIA) is True
        assert is_supported_chain(DEVNET_LOCAL) is False

    def test_supported_chains_without_a_network_lists_only_production(self) -> None:
        assert supported_chains() == (BASE_SEPOLIA,)

    def test_load_mandate_without_a_network_accepts_base_sepolia(self) -> None:
        mandate = load_mandate(mandate_doc(BASE_SEPOLIA))
        assert mandate.chain == BASE_SEPOLIA

    def test_chain_for_network_pairs_with_the_chain_id(self) -> None:
        assert chain_for_network(NETWORK_PRODUCTION) == BASE_SEPOLIA
        assert chain_for_network(NETWORK_DEVNET) == DEVNET_LOCAL

    @pytest.mark.parametrize("value", [None, 1, [], {}, object()])
    def test_a_non_string_chain_is_a_chain_error_not_a_type_error(self, value: object) -> None:
        """``in`` atas dict melempar TypeError untuk list dan dict.

        Versi pertama perubahan ini melewatkan penjaga ``isinstance`` dan dua
        tes di ``test_chains.py`` menangkapnya. Penjaga itu dinyatakan lagi di
        sini, untuk kedua jaringan.
        """
        with pytest.raises(ChainError):
            chain_id_for(value, network=NETWORK_PRODUCTION)  # type: ignore[arg-type]
        with pytest.raises(ChainError):
            chain_id_for(value, network=NETWORK_DEVNET)  # type: ignore[arg-type]


class TestMandateAcceptanceIsNetworkScoped:
    """(a), (b), dan (c)."""

    def test_a_devnet_mandate_is_accepted_on_devnet(self) -> None:
        mandate = load_mandate(mandate_doc(DEVNET_LOCAL), network=NETWORK_DEVNET)
        assert mandate.chain == DEVNET_LOCAL

    def test_a_devnet_mandate_is_refused_on_production(self) -> None:
        with pytest.raises(MandateError) as info:
            load_mandate(mandate_doc(DEVNET_LOCAL), network=NETWORK_PRODUCTION)
        pesan = str(info.value)
        assert DEVNET_LOCAL in pesan
        # Pesannya harus menyebut daftar chain PRODUKSI, bukan daftar devnet.
        assert BASE_SEPOLIA in pesan

    def test_a_devnet_mandate_is_refused_by_default(self) -> None:
        """Bawaan produksi: lupa menyebut jaringan berarti menolak devnet."""
        with pytest.raises(MandateError):
            load_mandate(mandate_doc(DEVNET_LOCAL))

    def test_a_production_mandate_is_refused_on_devnet(self) -> None:
        with pytest.raises(MandateError) as info:
            load_mandate(mandate_doc(BASE_SEPOLIA), network=NETWORK_DEVNET)
        pesan = str(info.value)
        assert BASE_SEPOLIA in pesan
        assert DEVNET_LOCAL in pesan

    def test_the_refusal_names_the_network(self) -> None:
        with pytest.raises(MandateError) as info:
            load_mandate(mandate_doc(BASE_SEPOLIA), network=NETWORK_DEVNET)
        assert NETWORK_DEVNET in str(info.value)


class TestEnvelopeCarriesTheNetworkChainId:
    """(d)."""

    @staticmethod
    def _payload(*, chain: str, network: str) -> Any:
        return build_payload(
            decision_id="11111111-1111-4111-8111-111111111111",
            mandate_id="m-devnet-1",
            agent_id="devnet-agent",
            recipient="0x" + "11" * 20,
            amount_base=10_000_000,
            issued_at=1_700_000_000,
            valid_until=1_700_000_300,
            mandate_hash="a" * 64,
            chain=chain,
            network=network,
        )

    def test_a_devnet_envelope_carries_31337(self) -> None:
        payload = self._payload(chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        assert payload.chain_id == 31337

    def test_a_production_envelope_carries_84532(self) -> None:
        payload = self._payload(chain=BASE_SEPOLIA, network=NETWORK_PRODUCTION)
        assert payload.chain_id == 84532

    def test_the_default_network_carries_84532(self) -> None:
        payload = build_payload(
            decision_id="11111111-1111-4111-8111-111111111111",
            mandate_id="m-devnet-1",
            agent_id="devnet-agent",
            recipient="0x" + "11" * 20,
            amount_base=10_000_000,
            issued_at=1_700_000_000,
            valid_until=1_700_000_300,
            mandate_hash="a" * 64,
        )
        assert payload.chain_id == 84532

    def test_a_devnet_chain_on_the_production_network_is_refused(self) -> None:
        with pytest.raises((ChainError, SigningError)):
            self._payload(chain=DEVNET_LOCAL, network=NETWORK_PRODUCTION)

    def test_a_production_chain_on_the_devnet_network_is_refused(self) -> None:
        with pytest.raises((ChainError, SigningError)):
            self._payload(chain=BASE_SEPOLIA, network=NETWORK_DEVNET)


class TestVerificationRefusesTheWrongChainId:
    """(e), dua arah."""

    @staticmethod
    def _signed(*, chain: str, network: str) -> tuple[Any, Any]:
        payload = build_payload(
            decision_id="22222222-2222-4222-8222-222222222222",
            mandate_id="m-devnet-1",
            agent_id="devnet-agent",
            recipient="0x" + "11" * 20,
            amount_base=10_000_000,
            issued_at=1_700_000_000,
            valid_until=1_700_000_300,
            mandate_hash="b" * 64,
            chain=chain,
            network=network,
        )
        return sign(payload, guard_key(), KEY_ID), registry()

    def test_a_devnet_envelope_verifies_on_devnet(self) -> None:
        envelope, registry = self._signed(chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        payload = verify(envelope, registry, chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        assert payload.chain_id == 31337

    def test_a_devnet_envelope_is_refused_on_production(self) -> None:
        envelope, registry = self._signed(chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        with pytest.raises(VerificationError) as info:
            verify(envelope, registry)
        pesan = str(info.value)
        assert "31337" in pesan
        assert "84532" in pesan

    def test_a_production_envelope_is_refused_on_devnet(self) -> None:
        envelope, registry = self._signed(chain=BASE_SEPOLIA, network=NETWORK_PRODUCTION)
        with pytest.raises(VerificationError) as info:
            verify(envelope, registry, chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        pesan = str(info.value)
        assert "84532" in pesan
        assert "31337" in pesan

    def test_the_refusal_names_the_network(self) -> None:
        envelope, registry = self._signed(chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        with pytest.raises(VerificationError) as info:
            verify(envelope, registry)
        assert NETWORK_PRODUCTION in str(info.value)
