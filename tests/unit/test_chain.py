"""Jembatan chain: encoding, kunci wallet, transaksi, dan pemeriksaan start.

PRD v2 bagian 9, PRD v2.1 bagian 9, Security v1.1 I1.

Tidak ada tes di berkas ini yang menyentuh jaringan. Yang diuji di sini adalah
bagian yang justru **tidak** butuh jaringan: byte apa yang dikirim, dan apakah
bahan kunci bisa bocor ke tempat yang dilihat manusia.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account

from fake_chain import TEST_WALLET_ADDRESS, TEST_WALLET_SEED, tx_hash_of, write_test_wallet
from payment_guard.addresses import to_checksum_address
from payment_guard.chain import (
    DECIMALS_SELECTOR,
    REQUIRED_DECIMALS,
    TRANSFER_SELECTOR,
    ChainError,
    Fees,
    Receipt,
    SignedTransaction,
    TransactionPlan,
    WalletIdentity,
    check_startup,
    decode_uint256,
    encode_transfer,
    load_wallet,
    selector_of,
)

RECIPIENT = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
USDC = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
CHAIN_ID = 84532

#: Calldata untuk ``transfer(0x1111...1111, 25_000_000)``.
#:
#: Nilai ini **tidak** dihitung oleh kode yang diuji. Ia disalin dari keluaran
#: ``web3.eth.contract(...).encode_abi("transfer", ...)``, yaitu encoder yang
#: dipakai hampir semua klien Ethereum. Known answer dari implementasi lain
#: adalah satu-satunya cara tes encoding bisa menangkap kesalahan bersama.
WEB3_REFERENCE_CALLDATA = (
    "0xa9059cbb"
    "0000000000000000000000001111111111111111111111111111111111111111"
    "00000000000000000000000000000000000000000000000000000000017d7840"
)


@pytest.fixture
def wallet(tmp_path: Path) -> WalletIdentity:
    """Wallet uji dari berkas di ``tmp_path``, di luar akar repo."""
    return load_wallet(write_test_wallet(tmp_path / "di-luar-repo"))


class TestSelectors:
    """Selector adalah empat byte yang menentukan fungsi mana yang dipanggil."""

    def test_transfer_selector_is_the_known_value(self) -> None:
        """``a9059cbb`` bisa dicocokkan dengan ERC-20 mana pun di explorer."""
        assert TRANSFER_SELECTOR.hex() == "a9059cbb"

    def test_decimals_selector_is_the_known_value(self) -> None:
        assert DECIMALS_SELECTOR.hex() == "313ce567"

    def test_selector_is_four_bytes(self) -> None:
        assert len(TRANSFER_SELECTOR) == 4
        assert len(DECIMALS_SELECTOR) == 4

    def test_a_different_signature_gives_a_different_selector(self) -> None:
        assert selector_of("transferFrom(address,address,uint256)") != TRANSFER_SELECTOR

    def test_selector_is_keccak_not_sha3(self) -> None:
        """SHA3-256 yang distandarkan NIST memberi nilai lain untuk input sama.

        Kalau backend hash tertukar, setiap transaksi akan memanggil fungsi
        yang tidak ada, dan ini satu-satunya tes yang menangkapnya di sini.
        """
        import hashlib

        nist = hashlib.sha3_256(b"transfer(address,uint256)").digest()[:4]
        assert TRANSFER_SELECTOR != nist


class TestEncodeTransfer:
    def test_matches_the_web3_encoder(self) -> None:
        calldata = encode_transfer("0x" + "11" * 20, 25_000_000)
        assert "0x" + calldata.hex() == WEB3_REFERENCE_CALLDATA

    def test_length_is_selector_plus_two_words(self) -> None:
        assert len(encode_transfer(RECIPIENT, 1)) == 4 + 32 + 32

    def test_address_is_left_padded_with_zeroes(self) -> None:
        calldata = encode_transfer(RECIPIENT, 1)
        assert calldata[4:16] == b"\x00" * 12
        assert calldata[16:36].hex() == RECIPIENT[2:]

    def test_amount_is_big_endian(self) -> None:
        calldata = encode_transfer(RECIPIENT, 258)
        assert calldata[-2:] == b"\x01\x02"

    def test_mixed_case_recipient_must_pass_checksum(self) -> None:
        """Alamat campuran huruf mengaku ber-checksum; klaim itu diperiksa."""
        with pytest.raises(ChainError):
            encode_transfer("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", 1)

    def test_checksummed_recipient_is_accepted_and_normalized(self) -> None:
        # Dihitung, bukan diketik. Bentuk ber-checksum tidak bisa ditebak dari
        # membaca alamatnya, dan literal yang salah akan membuat tes ini gagal
        # dengan alasan yang terlihat seperti bug di kode yang diuji.
        benar = to_checksum_address(RECIPIENT)
        assert benar != RECIPIENT
        assert encode_transfer(benar, 1) == encode_transfer(RECIPIENT, 1)

    @pytest.mark.parametrize(
        "buruk", ["", "0x", "bukan alamat", "0x" + "1" * 39, "0x" + "1" * 41, "0x" + "z" * 40]
    )
    def test_malformed_address_is_refused(self, buruk: str) -> None:
        with pytest.raises(ChainError):
            encode_transfer(buruk, 1)

    @pytest.mark.parametrize("jumlah", [0, -1, -25_000_000])
    def test_non_positive_amount_is_refused(self, jumlah: int) -> None:
        with pytest.raises(ChainError):
            encode_transfer(RECIPIENT, jumlah)

    def test_amount_above_uint256_is_refused(self) -> None:
        with pytest.raises(ChainError):
            encode_transfer(RECIPIENT, 2**256)

    def test_the_largest_uint256_is_accepted(self) -> None:
        assert len(encode_transfer(RECIPIENT, 2**256 - 1)) == 68

    @pytest.mark.parametrize("bukan_int", [True, "25", None, (1,)])
    def test_non_integer_amount_is_refused(self, bukan_int: object) -> None:
        """``True`` termasuk: di Python ia subclass ``int``."""
        with pytest.raises(ChainError):
            encode_transfer(RECIPIENT, bukan_int)  # type: ignore[arg-type]


class TestDecodeUint256:
    def test_reads_one_word(self) -> None:
        assert decode_uint256((6).to_bytes(32, "big")) == 6

    def test_reads_the_largest_value(self) -> None:
        assert decode_uint256(b"\xff" * 32) == 2**256 - 1

    @pytest.mark.parametrize("panjang", [0, 31, 33, 64])
    def test_wrong_length_is_refused(self, panjang: int) -> None:
        with pytest.raises(ChainError):
            decode_uint256(b"\x00" * panjang)

    def test_non_bytes_is_refused(self) -> None:
        with pytest.raises(ChainError):
            decode_uint256("0x06")  # type: ignore[arg-type]


class TestLoadWallet:
    def test_loads_the_address_derived_from_the_key(self, tmp_path: Path) -> None:
        hasil = load_wallet(write_test_wallet(tmp_path / "luar"))
        assert hasil.address == TEST_WALLET_ADDRESS

    def test_missing_file_is_an_error_not_a_crash(self, tmp_path: Path) -> None:
        with pytest.raises(ChainError, match="tidak bisa dibaca"):
            load_wallet(tmp_path / "tidak-ada.json")

    def test_not_json_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        path.write_text("bukan json", encoding="utf-8")
        with pytest.raises(ChainError, match="bukan JSON"):
            load_wallet(path)

    @pytest.mark.parametrize(
        "dokumen",
        [
            {"address": TEST_WALLET_ADDRESS},
            {"private_key": TEST_WALLET_SEED},
            {"address": TEST_WALLET_ADDRESS, "private_key": TEST_WALLET_SEED, "extra": 1},
            {},
        ],
    )
    def test_field_set_must_be_exact(self, tmp_path: Path, dokumen: dict) -> None:
        path = tmp_path / "w.json"
        path.write_text(json.dumps(dokumen), encoding="utf-8")
        with pytest.raises(ChainError, match="harus memuat tepat"):
            load_wallet(path)

    @pytest.mark.parametrize(
        "bahan",
        [
            "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # tanpa 0x
            "0xac0974",  # terlalu pendek
            "0x" + "a" * 65,  # terlalu panjang
            "0x" + "z" * 64,  # bukan hex
            "",
        ],
    )
    def test_malformed_private_key_is_refused(self, tmp_path: Path, bahan: str) -> None:
        path = tmp_path / "w.json"
        path.write_text(
            json.dumps({"address": TEST_WALLET_ADDRESS, "private_key": bahan}), encoding="utf-8"
        )
        with pytest.raises(ChainError, match="64 karakter heksadesimal"):
            load_wallet(path)

    def test_address_that_does_not_match_the_key_is_refused(self, tmp_path: Path) -> None:
        """Berkas tertukar tertangkap di sini, bukan nanti di chain."""
        path = tmp_path / "w.json"
        path.write_text(
            json.dumps({"address": "0x" + "22" * 20, "private_key": TEST_WALLET_SEED}),
            encoding="utf-8",
        )
        with pytest.raises(ChainError, match="tidak cocok dengan kuncinya"):
            load_wallet(path)

    def test_zero_key_is_refused_by_the_curve(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        path.write_text(
            json.dumps({"address": TEST_WALLET_ADDRESS, "private_key": "0x" + "00" * 32}),
            encoding="utf-8",
        )
        with pytest.raises(ChainError):
            load_wallet(path)


class TestKeyMaterialNeverLeaks:
    """Security v1.1 I1: bahan kunci tidak muncul di repr, pesan, atau log."""

    def test_wallet_repr_hides_the_key(self, wallet: WalletIdentity) -> None:
        assert TEST_WALLET_SEED[2:].lower() not in repr(wallet).lower()

    def test_wallet_repr_still_shows_the_address(self, wallet: WalletIdentity) -> None:
        """Alamat memang harus terlihat: ia publik dan dibutuhkan operator."""
        assert TEST_WALLET_ADDRESS in repr(wallet)

    def test_signed_transaction_repr_hides_the_raw_bytes(self, wallet: WalletIdentity) -> None:
        signed = wallet.sign(_transaction(wallet))
        assert signed.raw_tx.hex() not in repr(signed)

    def test_signed_transaction_repr_shows_the_hash(self, wallet: WalletIdentity) -> None:
        signed = wallet.sign(_transaction(wallet))
        assert signed.tx_hash in repr(signed)

    @pytest.mark.parametrize(
        "dokumen",
        [
            {"address": TEST_WALLET_ADDRESS, "private_key": "0x" + "z" * 64},
            {"address": "0x" + "22" * 20, "private_key": TEST_WALLET_SEED},
            {"address": TEST_WALLET_ADDRESS},
        ],
    )
    def test_refusal_messages_never_quote_the_key(self, tmp_path: Path, dokumen: dict) -> None:
        path = tmp_path / "w.json"
        path.write_text(json.dumps(dokumen), encoding="utf-8")
        with pytest.raises(ChainError) as info:
            load_wallet(path)
        pesan = str(info.value)
        assert TEST_WALLET_SEED[2:].lower() not in pesan.lower()
        assert dokumen.get("private_key", "x") not in pesan


class TestSigning:
    def test_hash_is_keccak_of_the_raw_bytes(self, wallet: WalletIdentity) -> None:
        """Sifat yang membuat write-ahead bisa dipercaya.

        ``tx_hash`` ditulis ke database **sebelum** broadcast. Kalau ia bukan
        hash dari byte yang disiarkan, baris write-ahead menunjuk transaksi
        yang salah dan recovery akan mencari sesuatu yang tidak pernah ada.
        """
        signed = wallet.sign(_transaction(wallet))
        assert signed.tx_hash == tx_hash_of(signed.raw_tx)

    def test_hash_shape_matches_the_database_check(self, wallet: WalletIdentity) -> None:
        """CHECK di ``0001_init.sql``: ``^0x[0-9a-f]{64}$``."""
        import re

        signed = wallet.sign(_transaction(wallet))
        assert re.fullmatch(r"0x[0-9a-f]{64}", signed.tx_hash)

    def test_nonce_is_carried_through(self, wallet: WalletIdentity) -> None:
        signed = wallet.sign(_transaction(wallet, nonce=42))
        assert signed.nonce == 42

    def test_the_same_transaction_signs_to_the_same_bytes(self, wallet: WalletIdentity) -> None:
        """Determinisme inilah yang membuat rebroadcast tidak membayar dua kali."""
        a = wallet.sign(_transaction(wallet))
        b = wallet.sign(_transaction(wallet))
        assert a.raw_tx == b.raw_tx
        assert a.tx_hash == b.tx_hash

    def test_a_different_nonce_gives_a_different_hash(self, wallet: WalletIdentity) -> None:
        a = wallet.sign(_transaction(wallet, nonce=1))
        b = wallet.sign(_transaction(wallet, nonce=2))
        assert a.tx_hash != b.tx_hash

    def test_a_malformed_transaction_is_an_error_not_a_crash(self, wallet: WalletIdentity) -> None:
        with pytest.raises(ChainError):
            wallet.sign({"nonce": 1})

    def test_non_integer_nonce_is_refused(self, wallet: WalletIdentity) -> None:
        transaksi = _transaction(wallet)
        transaksi["nonce"] = "1"
        with pytest.raises(ChainError):
            wallet.sign(transaksi)


class TestTransactionPlan:
    def plan(self) -> TransactionPlan:
        return TransactionPlan(
            to=USDC,
            data=encode_transfer(RECIPIENT, 25_000_000),
            value=0,
            chain_id=CHAIN_ID,
            nonce=3,
            gas=70_000,
            max_fee_per_gas=2_000_000_000,
            max_priority_fee_per_gas=1,
        )

    def test_recipient_is_in_the_calldata_not_the_to_field(self) -> None:
        """``to`` adalah kontrak token; penerimanya ada di dalam calldata.

        Membalik keduanya akan mengirim ETH ke penerima dan tidak memindahkan
        token sama sekali.
        """
        transaksi = self.plan().as_transaction()
        assert transaksi["to"].lower() == USDC
        assert RECIPIENT[2:] in str(transaksi["data"])

    def test_value_is_zero(self) -> None:
        assert self.plan().as_transaction()["value"] == 0

    def test_to_is_checksummed(self) -> None:
        """Node menolak alamat yang tidak ber-checksum."""
        transaksi = self.plan().as_transaction()
        assert transaksi["to"] != USDC
        assert transaksi["to"].lower() == USDC

    def test_data_is_hex_with_prefix(self) -> None:
        assert str(self.plan().as_transaction()["data"]).startswith("0xa9059cbb")

    def test_field_names_are_the_ones_eth_account_expects(self) -> None:
        assert set(self.plan().as_transaction()) == {
            "to",
            "data",
            "value",
            "chainId",
            "nonce",
            "gas",
            "maxFeePerGas",
            "maxPriorityFeePerGas",
        }

    def test_repr_hides_the_calldata(self) -> None:
        """Calldata tidak rahasia, tapi ia panjang dan membuat log tidak terbaca."""
        assert "a9059cbb" not in repr(self.plan())


class TestReceipt:
    def test_status_one_is_not_reverted(self) -> None:
        assert Receipt(tx_hash="0x" + "a" * 64, status=1, block_number=1).reverted is False

    @pytest.mark.parametrize("status", [0, 2])
    def test_any_other_status_is_reverted(self, status: int) -> None:
        assert Receipt(tx_hash="0x" + "a" * 64, status=status, block_number=1).reverted is True


class TestCheckStartup:
    def client(self, **overrides: object):
        from fake_chain import FakeChain

        return FakeChain(**overrides)  # type: ignore[arg-type]

    def run(self, client, *, minimum: int = 10**15):
        return check_startup(
            client,
            expected_chain_id=CHAIN_ID,
            usdc_address=USDC,
            wallet_address=TEST_WALLET_ADDRESS,
            minimum_balance_wei=minimum,
        )

    def test_a_healthy_node_passes(self) -> None:
        laporan = self.run(self.client())
        assert laporan.chain_id == CHAIN_ID
        assert laporan.decimals == REQUIRED_DECIMALS
        assert laporan.warnings == ()

    def test_wrong_chain_id_refuses_to_start(self) -> None:
        with pytest.raises(ChainError, match="chain id dari RPC"):
            self.run(self.client(chain_id_value=8453))

    @pytest.mark.parametrize("desimal", [0, 8, 18])
    def test_wrong_decimals_refuses_to_start(self, desimal: int) -> None:
        """Desimal lain berarti setiap jumlah di sistem ini punya arti lain."""
        with pytest.raises(ChainError, match="decimals"):
            self.run(self.client(decimals_value=desimal))

    def test_chain_id_is_checked_before_decimals(self) -> None:
        """Node yang salah jaringan tidak perlu ditanyai soal kontraknya."""
        client = self.client(chain_id_value=1, decimals_value=18)
        with pytest.raises(ChainError):
            self.run(client)
        assert "token_decimals" not in client.calls

    def test_low_balance_warns_and_still_starts(self) -> None:
        """Saldo kurang tidak boleh menghalangi ``recover`` dijalankan."""
        laporan = self.run(self.client(balance_wei_value=1), minimum=10**15)
        assert len(laporan.warnings) == 1
        assert "saldo ETH" in laporan.warnings[0]

    def test_zero_balance_still_starts(self) -> None:
        assert self.run(self.client(balance_wei_value=0)).warnings

    def test_exactly_the_minimum_does_not_warn(self) -> None:
        assert self.run(self.client(balance_wei_value=10**15), minimum=10**15).warnings == ()

    def test_the_warning_names_the_wallet(self) -> None:
        laporan = self.run(self.client(balance_wei_value=0))
        assert TEST_WALLET_ADDRESS in laporan.warnings[0]


def _transaction(wallet: WalletIdentity, *, nonce: int = 7) -> dict[str, object]:
    return TransactionPlan(
        to=USDC,
        data=encode_transfer(RECIPIENT, 25_000_000),
        value=0,
        chain_id=CHAIN_ID,
        nonce=nonce,
        gas=70_000,
        max_fee_per_gas=2_000_000_000,
        max_priority_fee_per_gas=1,
    ).as_transaction()


def test_the_test_seed_is_the_account_it_claims_to_be() -> None:
    """Penjaga untuk berkas fake: alamat uji memang milik seed uji.

    Kalau keduanya menyimpang, tes yang membandingkan ``from_addr`` akan gagal
    dengan alasan yang membingungkan.
    """
    assert Account.from_key(TEST_WALLET_SEED).address.lower() == TEST_WALLET_ADDRESS


def test_fees_is_a_plain_pair_of_integers() -> None:
    """Tidak ada float di jalur ongkos, seperti di jalur uang."""
    fees = Fees(max_fee_per_gas=2_000_000_000, max_priority_fee_per_gas=1)
    assert isinstance(fees.max_fee_per_gas, int)
    assert isinstance(fees.max_priority_fee_per_gas, int)


def test_signed_transaction_carries_bytes_not_hex() -> None:
    signed = SignedTransaction(tx_hash="0x" + "a" * 64, raw_tx=b"\x02\x03", nonce=0)
    assert isinstance(signed.raw_tx, bytes)
