"""Jembatan ke chain: encoding ERC-20, tanda tangan transaksi, klien RPC.

PRD v2 bagian 5 dan 9, PRD v2.1 bagian 9, Arsitektur v1.1 bagian 2 dan 8.

Modul ini memisahkan dua hal yang mudah tercampur:

**Encoding dan tanda tangan** tidak butuh jaringan. ``transfer(to, value)``
dirakit di sini dari dua nilai dan tidak lebih, dan transaksinya ditandatangani
secara lokal. Karena itu seluruh jalur yang menentukan *apa* yang dikirim bisa
diuji tanpa RPC sama sekali.

**Klien RPC** butuh jaringan, dan hanya menyediakan angka yang tidak bisa
diketahui secara lokal: ``chain_id``, nonce, gas, ongkos, dan receipt. Ia
bersembunyi di belakang :class:`ChainClient`, jadi tes memakai klien tiruan dan
tidak pernah menyentuh jaringan.

Dua catatan yang membentuk kode di sini
---------------------------------------

*Calldata dirakit sendiri, bukan lewat objek kontrak web3.* Sebuah ERC-20
``transfer`` adalah empat byte selector dan dua word; merakitnya sendiri
membuat satu-satunya tempat yang menentukan tujuan dan jumlah bisa dibaca utuh
dalam dua puluh baris, dan membuat known-answer test mungkin. Selector-nya
dihitung dari :func:`payment_guard.hashing.keccak256`, **bukan** SHA3-256 dari
stdlib: Ethereum memakai Keccak-256 dengan padding aslinya.

*Kunci wallet tidak pernah keluar dari modul ini.* :class:`WalletIdentity`
menyembunyikan bahan kuncinya dari ``repr``, dan tanda tangan dikerjakan lewat
method akun, sehingga tidak ada atribut kunci yang perlu dibaca pemanggil.
Nama fieldnya ``signer``, mengikuti kebiasaan yang sudah dipakai di modul
Guard: ketika field semacam itu dinamai ``key``, gitleaks membaca anotasi tipe
di sebelahnya sebagai rahasia ber-entropi tinggi dan menolak commit.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, cast

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from web3 import Web3
from web3.exceptions import BlockNotFound, TransactionNotFound, Web3Exception
from web3.types import TxParams

from payment_guard.addresses import AddressError, normalize_address, to_checksum_address
from payment_guard.chains import (
    NETWORK_ENV_VAR,
    NETWORK_PRODUCTION,
    is_anvil_default_address,
    is_devnet_network,
)
from payment_guard.db import endpoint_host
from payment_guard.hashing import keccak256
from payment_guard.money import BaseUnits

# --------------------------------------------------------------------------- #
# Konstanta ABI
# --------------------------------------------------------------------------- #

#: Tanda tangan fungsi ERC-20 yang dipakai. Daftar tertutup: dua fungsi, dan
#: keduanya hanya karena ada yang menuntutnya. ``transfer`` untuk pembayaran,
#: ``decimals`` untuk pemeriksaan start.
TRANSFER_SIGNATURE: Final = "transfer(address,uint256)"
DECIMALS_SIGNATURE: Final = "decimals()"

#: Tanda tangan event ERC-20 ``Transfer``, yang dipantau Watcher.
#:
#: Bentuknya **tanpa** nama parameter dan tanpa spasi: itulah bentuk yang
#: di-keccak untuk menjadi ``topic0``. ``Transfer(address from, address to,
#: uint256 value)`` akan memberi hash yang berbeda dan tidak pernah cocok
#: dengan satu log pun.
TRANSFER_EVENT_SIGNATURE: Final = "Transfer(address,address,uint256)"

#: Lebar satu word ABI, dalam byte.
WORD_BYTES: Final = 32

#: Panjang selector fungsi, dalam byte.
SELECTOR_BYTES: Final = 4

#: Panjang alamat, dalam byte.
ADDRESS_BYTES: Final = 20

#: Nilai maksimum ``uint256``.
UINT256_MAX: Final = 2 ** (WORD_BYTES * 8) - 1

#: Desimal yang WAJIB dimiliki kontrak USDC. Kontrak dengan desimal lain
#: berarti "25" di mandate dan "25000000" di payload tidak lagi berarti jumlah
#: yang sama, jadi Executor menolak jalan (PRD v2.1 bagian 9).
REQUIRED_DECIMALS: Final = 6

#: Bentuk bahan kunci wallet yang diterima: 0x + 64 hex, kasus apa pun.
PRIVATE_KEY_PATTERN: Final = re.compile(r"\A0x[0-9a-fA-F]{64}\Z")

#: Bentuk hash transaksi, cocok dengan CHECK di ``0001_init.sql``.
TX_HASH_PATTERN: Final = re.compile(r"\A0x[0-9a-f]{64}\Z")

#: Field wajib di berkas kunci wallet Executor. Daftar tertutup.
WALLET_KEY_FIELDS: Final[frozenset[str]] = frozenset({"address", "private_key"})

#: Pengali batas gas atas hasil ``eth_estimateGas``: 12/10, yaitu 20 persen.
#: Dihitung dengan bilangan bulat; tidak ada ``float`` di jalur mana pun.
GAS_LIMIT_NUMERATOR: Final = 12
GAS_LIMIT_DENOMINATOR: Final = 10

#: Pengali base fee untuk ``maxFeePerGas``. Dua kali base fee ditambah tip
#: adalah anjuran EIP-1559 yang biasa: ia menahan kenaikan base fee beberapa
#: blok tanpa menaikkan ongkos yang benar-benar dibayar.
BASE_FEE_MULTIPLIER: Final = 2

#: Batas waktu **menyambung**, dalam detik. Pendek dengan sengaja: port yang
#: tertutup atau host yang salah tidak perlu ditunggu selama batas baca.
DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS: Final = 5

#: Batas waktu **membaca** jawaban, dalam detik.
DEFAULT_RPC_READ_TIMEOUT_SECONDS: Final = 20

#: Jumlah percobaan per panggilan RPC, termasuk percobaan pertama.
RPC_MAX_ATTEMPTS: Final = 3

#: Jeda sebelum percobaan ke-2 dan ke-3, dalam detik.
#:
#: Bilangan bulat dengan sengaja: tidak ada ``float`` di ``src/`` mana pun, dan
#: ada tes higiene yang menegakkannya. Jeda sub-detik tidak akan menolong
#: menghadapi node yang sedang tidak sehat.
RPC_BACKOFF_SECONDS: Final[tuple[int, ...]] = (1, 2)

#: Status receipt yang berarti transaksi berhasil.
RECEIPT_SUCCESS: Final = 1


#: Tipe hasil satu panggilan RPC. Dipakai :meth:`Web3Client._call`.
T = TypeVar("T")


class ChainError(RuntimeError):
    """Kegagalan di jalur chain: RPC, encoding, atau bahan kunci wallet.

    Pesannya ditampilkan ke operator, jadi ia tidak pernah memuat bahan kunci
    maupun URL RPC utuh; lihat :func:`payment_guard.db.endpoint_host`.
    """


# --------------------------------------------------------------------------- #
# Encoding ABI
# --------------------------------------------------------------------------- #


def selector_of(signature: str) -> bytes:
    """Empat byte pertama ``keccak256(signature)``."""
    return keccak256(signature.encode("ascii"))[:SELECTOR_BYTES]


#: Selector yang sudah dihitung. Nilainya diuji known-answer: ``transfer``
#: adalah ``0xa9059cbb`` dan ``decimals`` adalah ``0x313ce567``, dua nilai yang
#: bisa dicocokkan dengan kontrak ERC-20 mana pun di explorer.
TRANSFER_SELECTOR: Final = selector_of(TRANSFER_SIGNATURE)
DECIMALS_SELECTOR: Final = selector_of(DECIMALS_SIGNATURE)

#: ``topic0`` event ``Transfer``: keccak256 penuh, bukan empat byte pertama.
#:
#: Topic adalah satu word utuh; memotongnya seperti selector fungsi akan
#: menghasilkan filter yang tidak pernah cocok. Nilainya diuji known-answer
#: terhadap ``0xddf252ad...``, topic yang sama di setiap kontrak ERC-20.
TRANSFER_EVENT_TOPIC: Final = keccak256(TRANSFER_EVENT_SIGNATURE.encode("ascii"))

#: Jumlah topic yang WAJIB dimiliki log ``Transfer``: topic0, from, to.
#:
#: ``value`` tidak di-index, jadi ia ada di ``data`` dan bukan di topic. Log
#: dengan jumlah topic lain bukan ``Transfer`` ERC-20 standar, dan Watcher
#: menolaknya alih-alih menebak posisi field.
TRANSFER_TOPIC_COUNT: Final = 3


def encode_transfer(recipient: str, amount_base: BaseUnits) -> bytes:
    """Calldata untuk ``transfer(recipient, amount_base)``.

    Dua argumen, dan tidak ada argumen ketiga yang bisa disisipkan: itulah
    sebabnya fungsi ini ada terpisah. Apa pun yang bukan tujuan dan jumlah
    tidak punya jalan masuk ke transaksi.

    Args:
        recipient: alamat tujuan; divalidasi dan dinormalisasi.
        amount_base: jumlah dalam base units, ``int`` positif.

    Raises:
        ChainError: bila alamatnya tidak sah atau jumlahnya di luar
            ``uint256`` positif.
    """
    try:
        address = normalize_address(recipient)
    except AddressError as exc:
        raise ChainError(f"alamat tujuan tidak sah: {exc}") from None
    return TRANSFER_SELECTOR + _encode_address(address) + _encode_uint256(amount_base)


def decode_uint256(data: bytes) -> int:
    """Baca satu word ``uint256`` dari hasil ``eth_call``.

    Raises:
        ChainError: bila panjangnya bukan satu word.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise ChainError(f"hasil eth_call harus bytes, bukan {type(data).__name__}")
    raw = bytes(data)
    if len(raw) != WORD_BYTES:
        raise ChainError(f"hasil eth_call harus {WORD_BYTES} byte, dapat {len(raw)} byte")
    return int.from_bytes(raw, byteorder="big")


def _encode_address(address: str) -> bytes:
    """Alamat sebagai satu word: 12 byte nol lalu 20 byte alamat."""
    raw = bytes.fromhex(address[2:])
    return b"\x00" * (WORD_BYTES - ADDRESS_BYTES) + raw


def _encode_uint256(value: int) -> bytes:
    """Bilangan bulat positif sebagai satu word big-endian."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChainError(f"jumlah harus int, bukan {type(value).__name__}")
    if value <= 0:
        raise ChainError(f"jumlah harus lebih besar dari nol, dapat {value}")
    if value > UINT256_MAX:
        raise ChainError(f"jumlah {value} melebihi uint256")
    return value.to_bytes(WORD_BYTES, byteorder="big")


# --------------------------------------------------------------------------- #
# Kunci wallet Executor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WalletIdentity:
    """Wallet milik Executor: alamatnya, dan akun yang bisa menandatangani.

    ``repr`` sengaja tidak menampilkan akunnya: objek ini bisa muncul di
    traceback, dan ``LocalAccount`` membawa bahan kuncinya.
    """

    address: str
    signer: LocalAccount = field(repr=False)

    def sign(self, transaction: Mapping[str, object]) -> SignedTransaction:
        """Tandatangani ``transaction`` secara lokal. Tidak menyentuh jaringan.

        Raises:
            ChainError: bila eth-account menolak bentuk transaksinya.
        """
        # Disalin ke dict biasa dulu, lalu di-cast. eth-account dan web3
        # menuntut dua bentuk dict yang berbeda untuk transaksi yang sama, dan
        # keduanya memakai alias tipe internalnya sendiri. Nilai di dalam
        # ``payload`` sudah dibentuk :meth:`TransactionPlan.as_transaction`,
        # jadi ``cast`` di sini melintasi batas pustaka — ia tidak melewati
        # pemeriksaan milik kita. Bentuknya tetap dijaga di sisi kita: ``nonce``
        # diperiksa di bawah, dan ``to``, ``data``, serta ``value`` dibangun
        # dari :class:`TransactionPlan`, bukan dari masukan bebas.
        payload: dict[str, object] = dict(transaction)
        try:
            signed = self.signer.sign_transaction(cast("dict[str, Any]", payload))
        except (ValueError, TypeError) as exc:
            # Pesan eth-account bisa memuat isi transaksi, tapi tidak pernah
            # memuat kunci. Baris pertamanya cukup dan paling mudah dibaca.
            raise ChainError(f"transaksi tidak bisa ditandatangani: {_first_line(exc)}") from None
        nonce = transaction["nonce"]
        if isinstance(nonce, bool) or not isinstance(nonce, int):
            raise ChainError("nonce transaksi harus int")
        return SignedTransaction(
            tx_hash=_normalize_tx_hash(signed.hash),
            raw_tx=bytes(signed.raw_transaction),
            nonce=nonce,
        )


def load_wallet(path: Path, *, network: str = NETWORK_PRODUCTION) -> WalletIdentity:
    """Muat kunci wallet Executor dari berkas JSON di luar repo.

    Bentuk berkas::

        {"address": "0x...", "private_key": "0x<64 hex>"}

    ``address`` wajib ada **dan** wajib cocok dengan alamat yang diturunkan
    dari kuncinya. Field itu bukan hiasan: tanpa pemeriksaan ini, berkas yang
    tertukar akan membuat Executor menandatangani dari wallet yang salah dan
    baru terlihat setelah transaksi gagal di chain karena saldo nol, atau
    lebih buruk, berhasil dari wallet yang tidak dimaksud.

    Pesan error menyebut berkas dan field yang bermasalah, **tidak pernah**
    isinya.

    Raises:
        ChainError: bila berkasnya tidak bisa dibaca, bentuknya salah, atau
            alamatnya tidak cocok dengan kuncinya.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChainError(
            f"berkas kunci wallet tidak bisa dibaca ({path}): {exc.strerror}"
        ) from None

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChainError(f"berkas kunci wallet bukan JSON yang sah ({path}): {exc.msg}") from None

    if not isinstance(document, dict) or set(document) != WALLET_KEY_FIELDS:
        raise ChainError(
            f"berkas kunci wallet harus memuat tepat: "
            f"{', '.join(sorted(WALLET_KEY_FIELDS))} ({path})"
        )

    material = document["private_key"]
    if not isinstance(material, str) or PRIVATE_KEY_PATTERN.match(material) is None:
        raise ChainError(f"private_key harus '0x' diikuti 64 karakter heksadesimal ({path})")

    try:
        account = Account.from_key(material)
    except (ValueError, TypeError):
        # Pengecualian aslinya bisa mengutip bahan kuncinya; ia tidak diteruskan.
        raise ChainError(f"private_key bukan kunci secp256k1 yang sah ({path})") from None

    try:
        declared = normalize_address(document["address"])
    except AddressError as exc:
        raise ChainError(f"address di berkas kunci wallet tidak sah ({path}): {exc}") from None

    derived = normalize_address(account.address)
    if declared != derived:
        raise ChainError(
            f"address di berkas kunci wallet tidak cocok dengan kuncinya ({path}): "
            f"berkas menyebut {declared}, kuncinya menurunkan {derived}"
        )

    # Kunci default Anvil PUBLIK, dan itu membuatnya berbahaya di mana pun
    # selain devnet lokal. Penolakan ini ada karena urutan kejadian yang paling
    # mungkin bukan kejahatan melainkan kelalaian: operator menjalankan devnet,
    # lalu menjalankan Executor tanpa `PAYMENT_GUARD_NETWORK=devnet` dan masih
    # menunjuk ke berkas kunci devnet. Tanpa pemeriksaan ini, Executor akan
    # menandatangani di chain sungguhan dengan kunci yang diketahui publik.
    #
    # Yang dibandingkan alamatnya, bukan kuncinya: pesan error tidak boleh
    # pernah bisa mengutip bahan kunci, dan mengenali sudah cukup.
    if not is_devnet_network(network) and is_anvil_default_address(derived):
        raise ChainError(
            f"berkas kunci wallet memakai akun default Anvil ({derived}) di jaringan "
            f"{network!r}. Kunci itu PUBLIK — mnemonic-nya diterbitkan di dokumentasi "
            f"Foundry — jadi ia hanya boleh dipakai di devnet lokal. Setel "
            f"{NETWORK_ENV_VAR}=devnet bila ini memang devnet, atau pakai berkas kunci "
            f"lain. Lihat docs/devnet.md ({path})"
        )
    return WalletIdentity(address=derived, signer=account)


# --------------------------------------------------------------------------- #
# Transaksi
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fees:
    """Ongkos EIP-1559 untuk satu transaksi, dalam wei."""

    max_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True)
class TransferLog:
    """Satu log ``Transfer`` USDC, sudah didekode dan divalidasi.

    Bentuknya sengaja sama dengan kolom ``settlements`` di ``0001_init.sql``:
    modul ini yang mengubah jawaban node menjadi nilai yang bisa dipercaya,
    sehingga :mod:`payment_guard.watcher` tidak pernah menyentuh dict mentah
    dari web3. Alamat sudah lowercase ternormalisasi dan hash sudah dicek
    bentuknya, jadi CHECK di database tidak bisa dilanggar oleh log yang aneh.

    ``amount_base`` bertipe :data:`payment_guard.money.BaseUnits`, dan nilainya
    boleh **0**: ERC-20 mengizinkan transfer bernilai nol, dan ``settlements``
    mencerminkan apa yang ada di chain (``CHECK (amount_base >= 0)``), bukan
    apa yang kami izinkan (``executions`` memakai ``> 0``). Karena itu ia tidak
    dibuat lewat :func:`payment_guard.money.base_units`, yang menolak nol.
    """

    chain_id: int
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    from_addr: str
    to_addr: str
    amount_base: BaseUnits

    @property
    def unique_key(self) -> tuple[int, str, int]:
        """Kunci ``UNIQUE (chain_id, tx_hash, log_index)`` di ``settlements``."""
        return (self.chain_id, self.tx_hash, self.log_index)


@dataclass(frozen=True)
class Receipt:
    """Bagian receipt yang dipakai Executor, dan tidak lebih."""

    tx_hash: str
    status: int
    block_number: int

    @property
    def reverted(self) -> bool:
        """True bila transaksinya masuk blok tapi gagal dijalankan."""
        return self.status != RECEIPT_SUCCESS


@dataclass(frozen=True)
class SignedTransaction:
    """Hasil tanda tangan lokal: hash, byte mentah, dan nonce-nya.

    Ketiganya ada **sebelum** broadcast, dan itulah yang membuat write-ahead
    mungkin: baris ``executions`` bisa ditulis lengkap sebelum ada apa pun
    yang terkirim ke jaringan (PRD v2 bagian 9).
    """

    tx_hash: str
    raw_tx: bytes = field(repr=False)
    nonce: int


@dataclass(frozen=True)
class TransactionPlan:
    """Field transaksi yang akan ditandatangani.

    Objek ini dengan sengaja tidak punya field untuk apa pun yang tidak berasal
    dari payload envelope atau dari RPC. Tidak ada tempat untuk menyisipkan
    nilai dari sumber ketiga.
    """

    to: str
    data: bytes = field(repr=False)
    value: int
    chain_id: int
    nonce: int
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int

    def as_transaction(self) -> dict[str, object]:
        """Bentuk dict yang diterima eth-account.

        Alamat tujuan dikirim ber-checksum EIP-55: eth-account menolak alamat
        yang tidak ber-checksum, dan checksum dihitung di
        :mod:`payment_guard.addresses`, bukan oleh pustaka pihak ketiga.
        """
        return {
            "to": to_checksum_address(self.to),
            "data": "0x" + self.data.hex(),
            "value": self.value,
            "chainId": self.chain_id,
            "nonce": self.nonce,
            "gas": self.gas,
            "maxFeePerGas": self.max_fee_per_gas,
            "maxPriorityFeePerGas": self.max_priority_fee_per_gas,
        }


# --------------------------------------------------------------------------- #
# Klien RPC
# --------------------------------------------------------------------------- #


class ChainClient(Protocol):
    """Yang dibutuhkan Executor dari sebuah node, dan tidak lebih.

    Permukaannya sempit dengan sengaja: makin sedikit yang bisa ditanyakan ke
    jaringan, makin sedikit yang bisa mengubah keputusan. Tidak ada method
    untuk membaca saldo token, karena jatah ditegakkan dari ``decisions``, bukan
    dari apa yang kebetulan ada di wallet.
    """

    def chain_id(self) -> int: ...

    def token_decimals(self, token: str) -> int: ...

    def eth_balance(self, address: str) -> int: ...

    def pending_nonce(self, address: str) -> int: ...

    def confirmed_nonce(self, address: str) -> int: ...

    def fees(self) -> Fees: ...

    def estimate_gas(self, transaction: Mapping[str, object]) -> int: ...

    def send_raw_transaction(self, raw_tx: bytes) -> str: ...

    def receipt(self, tx_hash: str) -> Receipt | None: ...

    # --- Dibaca Watcher saja (M6). Semuanya read-only. ---

    def latest_block(self) -> int: ...

    def block_hash(self, block_number: int) -> str | None: ...

    def transfer_logs(
        self,
        token: str,
        *,
        from_addr: str,
        first_block: int,
        last_block: int,
    ) -> tuple[TransferLog, ...]: ...


class Web3Client:
    """:class:`ChainClient` di atas web3.py lewat HTTP JSON-RPC.

    URL RPC tidak pernah dicetak utuh: penyedia berbayar menaruh kunci API di
    path atau query, jadi pesan error hanya menyebut skema dan host.
    """

    def __init__(
        self,
        rpc_url: str,
        *,
        connect_timeout_seconds: int = DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: int = DEFAULT_RPC_READ_TIMEOUT_SECONDS,
        max_attempts: int = RPC_MAX_ATTEMPTS,
        sleep: Callable[[int], object] = time.sleep,
    ) -> None:
        self._host = endpoint_host(rpc_url)
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._web3 = Web3(
            Web3.HTTPProvider(
                rpc_url,
                # Pasangan (connect, read). requests menerima keduanya terpisah,
                # dan memberinya satu angka berarti menunggu selama batas baca
                # hanya untuk tahu bahwa portnya tertutup.
                request_kwargs={"timeout": (connect_timeout_seconds, read_timeout_seconds)},
            )
        )

    @property
    def host(self) -> str:
        """Skema dan host endpoint, aman dicetak."""
        return self._host

    def chain_id(self) -> int:
        return int(self._call("eth_chainId", lambda: self._web3.eth.chain_id))

    def token_decimals(self, token: str) -> int:
        call = cast(
            TxParams,
            {"to": _checksummed(token), "data": "0x" + DECIMALS_SELECTOR.hex()},
        )
        result = self._call("decimals()", lambda: self._web3.eth.call(call))
        return decode_uint256(bytes(result))

    def eth_balance(self, address: str) -> int:
        checksummed = _checksummed(address)
        return int(self._call("eth_getBalance", lambda: self._web3.eth.get_balance(checksummed)))

    def pending_nonce(self, address: str) -> int:
        checksummed = _checksummed(address)
        return int(
            self._call(
                "eth_getTransactionCount(pending)",
                lambda: self._web3.eth.get_transaction_count(checksummed, "pending"),
            )
        )

    def confirmed_nonce(self, address: str) -> int:
        checksummed = _checksummed(address)
        return int(
            self._call(
                "eth_getTransactionCount(latest)",
                lambda: self._web3.eth.get_transaction_count(checksummed, "latest"),
            )
        )

    def fees(self) -> Fees:
        tip = int(self._call("eth_maxPriorityFeePerGas", lambda: self._web3.eth.max_priority_fee))
        block = self._call("eth_getBlockByNumber", lambda: self._web3.eth.get_block("latest"))
        base = block.get("baseFeePerGas")
        if base is None:
            raise ChainError(
                f"blok terakhir dari {self._host} tidak punya baseFeePerGas; "
                "chain ini bukan EIP-1559"
            )
        return Fees(
            max_fee_per_gas=int(base) * BASE_FEE_MULTIPLIER + tip, max_priority_fee_per_gas=tip
        )

    def estimate_gas(self, transaction: Mapping[str, object]) -> int:
        payload = cast(TxParams, dict(transaction))
        estimate = int(self._call("eth_estimateGas", lambda: self._web3.eth.estimate_gas(payload)))
        return estimate * GAS_LIMIT_NUMERATOR // GAS_LIMIT_DENOMINATOR

    def send_raw_transaction(self, raw_tx: bytes) -> str:
        sent = self._call(
            "eth_sendRawTransaction", lambda: self._web3.eth.send_raw_transaction(raw_tx)
        )
        return _normalize_tx_hash(sent)

    def receipt(self, tx_hash: str) -> Receipt | None:
        def ask() -> object | None:
            try:
                return self._web3.eth.get_transaction_receipt(tx_hash)  # type: ignore[arg-type]
            except TransactionNotFound:
                # Bukan kegagalan: transaksinya memang belum masuk blok.
                # Ditangani di dalam aksi supaya ia tidak ikut di-retry.
                return None

        found = self._call("eth_getTransactionReceipt", ask)
        if found is None:
            return None
        receipt = cast(Mapping[str, object], found)
        return Receipt(
            tx_hash=_normalize_tx_hash(receipt["transactionHash"]),
            status=int(cast(int, receipt["status"])),
            block_number=int(cast(int, receipt["blockNumber"])),
        )

    # ----------------------------------------------------------------------- #
    # Dibaca Watcher saja (M6)
    # ----------------------------------------------------------------------- #

    def latest_block(self) -> int:
        return int(self._call("eth_blockNumber", lambda: self._web3.eth.block_number))

    def block_hash(self, block_number: int) -> str | None:
        """Hash kanonik blok ``block_number``, atau ``None`` bila belum ada.

        ``None`` berarti node belum punya blok setinggi itu — bukan kegagalan.
        Nilai inilah yang membedakan reorg dari "belum sampai": blok yang hash
        tersimpannya tidak sama dengan hash kanonik sudah berpindah rantai.
        """
        _require_block_number(block_number)

        def ask() -> object | None:
            try:
                return self._web3.eth.get_block(block_number)
            except BlockNotFound:
                return None

        found = self._call("eth_getBlockByNumber", ask)
        if found is None:
            return None
        block = cast(Mapping[str, object], found)
        return _normalize_block_hash(block["hash"])

    def transfer_logs(
        self,
        token: str,
        *,
        from_addr: str,
        first_block: int,
        last_block: int,
    ) -> tuple[TransferLog, ...]:
        """Log ``Transfer`` kontrak ``token`` dengan ``from`` = ``from_addr``.

        Penyaringan dilakukan **node**, lewat ``address`` dan ``topics``, bukan
        oleh kami setelah log diterima: rentang blok di Base bisa memuat
        ratusan ribu Transfer USDC, dan menyaring di sisi kami berarti menarik
        semuanya lewat jaringan untuk membuang hampir semuanya.

        ``topics = [topic0, from, None]`` — ``to`` dibiarkan terbuka dengan
        sengaja. Pemantauannya per PRD v2 bagian 10 adalah per **pengirim**,
        yaitu wallet Executor, karena transfer dari wallet itu ke tujuan yang
        tidak kami kenal justru temuan yang paling penting (``unmatched_onchain``
        di PRD v2 bagian 11). Menyaring per ``to`` akan membuat transfer semacam
        itu tidak pernah terlihat.

        Raises:
            ChainError: bila rentang bloknya tidak sah, alamatnya tidak sah,
                atau ada log yang tidak berbentuk ``Transfer`` ERC-20.
        """
        _require_block_number(first_block)
        _require_block_number(last_block)
        if last_block < first_block:
            raise ChainError(
                f"rentang blok tidak sah: {first_block}..{last_block} berakhir sebelum mulai"
            )
        try:
            token_address = normalize_address(token)
            sender = normalize_address(from_addr)
        except AddressError as exc:
            raise ChainError(f"alamat untuk eth_getLogs tidak sah: {exc}") from None

        criteria = {
            "address": _checksummed(token_address),
            "fromBlock": first_block,
            "toBlock": last_block,
            "topics": [
                "0x" + TRANSFER_EVENT_TOPIC.hex(),
                "0x" + _encode_address(sender).hex(),
                None,
            ],
        }
        raw = self._call(
            "eth_getLogs",
            lambda: self._web3.eth.get_logs(cast(Any, criteria)),
        )
        chain = self.chain_id()
        return tuple(
            decode_transfer_log(cast(Mapping[str, object], item), chain_id=chain) for item in raw
        )

    def _call(self, what: str, action: Callable[[], T]) -> T:
        """Jalankan satu panggilan RPC dengan batas percobaan dan jeda.

        Yang diulang hanya **kegagalan transport**: timeout, koneksi tertutup,
        DNS gagal. ``requests`` melemparkannya sebagai turunan ``OSError``, dan
        artinya kita tidak tahu apakah permintaannya sampai. Mengulang aman di
        kedua arah — pembacaan tidak punya efek samping, dan
        ``eth_sendRawTransaction`` dengan byte yang **sama** memberi ``tx_hash``
        yang sama, jadi ia tidak bisa menjadi dua pembayaran.

        Yang **tidak** diulang adalah jawaban protokol (``Web3Exception``):
        node sudah menjawab dan menolak, jadi percobaan berikutnya hanya akan
        mendapat penolakan yang sama. Mengulanginya cuma memperlambat kegagalan.

        Tidak ada jalur menunggu tanpa batas: paling banyak
        :data:`RPC_MAX_ATTEMPTS` percobaan, masing-masing dibatasi timeout
        connect dan read, dengan jeda dari :data:`RPC_BACKOFF_SECONDS`.

        Raises:
            ChainError: selalu, bila seluruh percobaan gagal — pesannya tidak
                pernah memuat URL RPC utuh.
        """
        terakhir = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                return action()
            except ChainError:
                raise
            except OSError as exc:
                terakhir = _first_line(exc)
                if attempt < self._max_attempts:
                    self._sleep(_backoff_for(attempt))
            except Web3Exception as exc:
                raise ChainError(f"{what} ditolak {self._host}: {_first_line(exc)}") from None
            except (ValueError, KeyError) as exc:
                raise ChainError(f"{what} gagal di {self._host}: {_first_line(exc)}") from None
        raise ChainError(
            f"{what} gagal di {self._host} setelah {self._max_attempts} percobaan: {terakhir}"
        )


def _checksummed(address: str) -> ChecksumAddress:
    """Alamat ber-checksum EIP-55 dalam tipe yang diminta web3.

    Checksum dihitung di :mod:`payment_guard.addresses` dengan Keccak-256 kami
    sendiri, bukan oleh web3. ``cast`` di sini hanya menyatakan ulang jaminan
    yang sudah diberikan fungsi itu; ia tidak melonggarkan pemeriksaan apa pun
    yang nyata.
    """
    return cast(ChecksumAddress, to_checksum_address(address))


def _backoff_for(attempt: int) -> int:
    """Jeda sebelum percobaan berikutnya, dalam detik.

    Percobaan di luar panjang :data:`RPC_BACKOFF_SECONDS` memakai jeda
    terakhirnya, jadi ``max_attempts`` yang besar tidak menyebabkan IndexError.
    """
    index = min(attempt - 1, len(RPC_BACKOFF_SECONDS) - 1)
    return RPC_BACKOFF_SECONDS[index]


# --------------------------------------------------------------------------- #
# Pemeriksaan start
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StartupReport:
    """Hasil pemeriksaan start Executor.

    ``warnings`` memuat hal yang tidak menggagalkan start. Saldo gas yang
    kurang masuk di sini dengan sengaja: ia menghentikan broadcast nanti
    dengan sendirinya, dan menolak start karenanya akan membuat ``recover``
    tidak bisa dijalankan justru saat ia paling dibutuhkan.
    """

    chain_id: int
    decimals: int
    wallet_address: str
    balance_wei: int
    warnings: tuple[str, ...]


def check_chain_and_token(
    client: ChainClient,
    *,
    expected_chain_id: int,
    usdc_address: str,
) -> tuple[int, int]:
    """Periksa chain id RPC dan ``decimals()`` token. Kembalikan keduanya.

    Dua pemeriksaan yang sama dibutuhkan Executor **dan** Watcher, dan
    keduanya menggagalkan start: chain yang salah berarti setiap hash menunjuk
    jaringan lain, dan ``decimals()`` yang bukan 6 berarti setiap angka punya
    arti lain. Yang membedakan keduanya adalah apa yang **tidak** diperiksa
    Watcher: ia tidak punya wallet dan tidak mengirim apa pun, jadi saldo gas
    bukan urusannya.

    Raises:
        ChainError: bila chain id tidak cocok atau ``decimals()`` bukan
            :data:`REQUIRED_DECIMALS`.
    """
    seen_chain_id = client.chain_id()
    if seen_chain_id != expected_chain_id:
        raise ChainError(
            f"chain id dari RPC adalah {seen_chain_id}, bukan {expected_chain_id}. "
            "Endpoint ini menunjuk jaringan lain; tidak ada yang dieksekusi."
        )

    decimals = client.token_decimals(usdc_address)
    if decimals != REQUIRED_DECIMALS:
        raise ChainError(
            f"kontrak {usdc_address} melaporkan decimals() = {decimals}, bukan "
            f"{REQUIRED_DECIMALS}. Seluruh sistem menghitung base units USDC "
            "dengan 6 desimal, jadi jumlahnya akan salah secara diam-diam."
        )
    return seen_chain_id, decimals


def check_startup(
    client: ChainClient,
    *,
    expected_chain_id: int,
    usdc_address: str,
    wallet_address: str,
    minimum_balance_wei: int,
) -> StartupReport:
    """Periksa chain id, ``decimals()``, dan saldo gas sebelum bekerja.

    Chain id yang salah dan ``decimals()`` yang bukan 6 **menggagalkan** start:
    keduanya berarti setiap angka sesudahnya punya arti lain. Saldo gas yang
    kurang hanya peringatan.

    Raises:
        ChainError: bila chain id tidak cocok atau ``decimals()`` bukan
            :data:`REQUIRED_DECIMALS`.
    """
    seen_chain_id, decimals = check_chain_and_token(
        client, expected_chain_id=expected_chain_id, usdc_address=usdc_address
    )

    balance = client.eth_balance(wallet_address)
    warnings: list[str] = []
    if balance < minimum_balance_wei:
        warnings.append(
            f"saldo ETH wallet {wallet_address} hanya {balance} wei, di bawah "
            f"{minimum_balance_wei} wei yang dianjurkan untuk gas. Broadcast bisa "
            "gagal; isi lewat faucet testnet."
        )
    return StartupReport(
        chain_id=seen_chain_id,
        decimals=decimals,
        wallet_address=wallet_address,
        balance_wei=balance,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Pembantu
# --------------------------------------------------------------------------- #


def _normalize_tx_hash(value: object) -> str:
    """Hash transaksi sebagai ``0x`` + 64 hex kecil.

    web3 mengembalikan ``HexBytes``, yang ``hex()``-nya pernah berubah bentuk
    antar versi (dengan dan tanpa awalan ``0x``). Normalisasi di satu tempat
    membuat nilai yang ditulis ke ``executions`` selalu lolos CHECK
    ``tx_hash ~ '^0x[0-9a-f]{64}$'``, apa pun versinya.

    Raises:
        ChainError: bila bentuknya bukan hash 32 byte.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        text = "0x" + bytes(value).hex()
    elif isinstance(value, str):
        text = value if value.startswith("0x") else "0x" + value
    else:
        raise ChainError(f"hash transaksi harus bytes atau str, bukan {type(value).__name__}")
    text = text.lower()
    if TX_HASH_PATTERN.match(text) is None:
        raise ChainError("hash transaksi tidak berbentuk '0x' + 64 hex")
    return text


def _normalize_block_hash(value: object) -> str:
    """Hash blok sebagai ``0x`` + 64 hex kecil.

    Bentuknya identik dengan hash transaksi — 32 byte — dan CHECK
    ``block_hash ~ '^0x[0-9a-f]{64}$'`` di ``settlements`` sama persis dengan
    yang ada di ``executions``, jadi normalisasinya dipakai ulang. Namanya
    tetap terpisah supaya pesan errornya menyebut hal yang benar: "hash
    transaksi tidak berbentuk..." untuk hash blok akan menyesatkan orang yang
    membacanya di jam tiga pagi.
    """
    try:
        return _normalize_tx_hash(value)
    except ChainError:
        raise ChainError("hash blok tidak berbentuk '0x' + 64 hex") from None


def _require_block_number(value: object) -> int:
    """Nomor blok sebagai ``int`` tak negatif.

    ``bool`` ditolak lebih dulu: ia turunan ``int``, dan ``True`` akan lolos
    menjadi blok 1. Nilai ini masuk ke ``eth_getLogs`` dan ke kolom
    ``BIGINT CHECK (block_number >= 0)``, jadi keduanya harus benar.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChainError(f"nomor blok harus int, bukan {type(value).__name__}")
    if value < 0:
        raise ChainError(f"nomor blok tidak boleh negatif, dapat {value}")
    return value


def decode_transfer_log(log: Mapping[str, object], *, chain_id: int) -> TransferLog:
    """Ubah satu log ``eth_getLogs`` mentah menjadi :class:`TransferLog`.

    Semua yang bisa salah diperiksa di sini, dan tidak ada yang ditebak. Log
    dari node adalah masukan tak terpercaya seperti masukan lain: ia menentukan
    baris yang ditulis ke ``settlements``, dan baris yang salah di situ berarti
    rekonsiliasi yang salah.

    ``value`` dibaca dari ``data``, bukan dari topic: ia tidak di-index di
    ERC-20. ``from`` dan ``to`` dibaca dari topic 1 dan 2, dan dinormalisasi
    lowercase supaya pembandingan dengan ``executions.to_addr`` adalah
    perbandingan string biasa, bukan perbandingan yang bergantung pada
    checksum.

    Raises:
        ChainError: bila jumlah topic salah, panjang ``data`` bukan satu word,
            alamatnya tidak sah, atau log masih ``pending`` (tanpa blok).
    """
    topics = log.get("topics")
    if not isinstance(topics, list | tuple) or len(topics) != TRANSFER_TOPIC_COUNT:
        count = len(topics) if isinstance(topics, list | tuple) else "bukan daftar"
        raise ChainError(
            f"log Transfer harus punya {TRANSFER_TOPIC_COUNT} topic, dapat {count}. "
            "Filter eth_getLogs mungkin salah, atau kontraknya bukan ERC-20 standar."
        )
    if _topic_bytes(topics[0]) != TRANSFER_EVENT_TOPIC:
        raise ChainError(
            "topic0 log bukan Transfer(address,address,uint256); filter node tidak dipatuhi"
        )

    block_number = log.get("blockNumber")
    if block_number is None or log.get("blockHash") is None:
        raise ChainError(
            "log masih pending (tanpa blok). Watcher hanya memproses blok yang sudah "
            "melewati kedalaman konfirmasi, jadi log tanpa blok tidak boleh sampai ke sini."
        )
    log_index = log.get("logIndex")
    if log_index is None:
        raise ChainError("log tanpa logIndex tidak bisa dijadikan kunci unik settlement")

    return TransferLog(
        chain_id=chain_id,
        tx_hash=_normalize_tx_hash(log["transactionHash"]),
        log_index=_require_log_index(log_index),
        block_number=_require_block_number(int(cast(int, block_number))),
        block_hash=_normalize_block_hash(log["blockHash"]),
        from_addr=_address_from_topic(topics[1], "from"),
        to_addr=_address_from_topic(topics[2], "to"),
        # Tanpa `base_units()`: transfer bernilai nol sah di ERC-20, dan
        # `settlements` mencatat apa yang terjadi di chain.
        amount_base=BaseUnits(decode_uint256(_topic_bytes(log.get("data")))),
    )


def _require_log_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChainError(f"logIndex harus int, bukan {type(value).__name__}")
    if value < 0:
        raise ChainError(f"logIndex tidak boleh negatif, dapat {value}")
    return value


def _topic_bytes(value: object) -> bytes:
    """Satu topic atau field ``data`` sebagai byte mentah."""
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, str):
        text = value[2:] if value.startswith("0x") else value
        try:
            return bytes.fromhex(text)
        except ValueError:
            raise ChainError("topic atau data log bukan hex yang sah") from None
    raise ChainError(f"topic atau data log harus bytes atau str, bukan {type(value).__name__}")


def _address_from_topic(value: object, label: str) -> str:
    """Alamat dari satu topic: 12 byte nol lalu 20 byte alamat.

    Byte padding diperiksa, tidak dilewati. Topic yang 12 byte pertamanya
    bukan nol bukan alamat yang di-pad — ia sesuatu yang lain, dan membaca 20
    byte terakhirnya akan memberi alamat yang terlihat masuk akal.
    """
    raw = _topic_bytes(value)
    if len(raw) != WORD_BYTES:
        raise ChainError(f"topic {label} harus {WORD_BYTES} byte, dapat {len(raw)} byte")
    padding = WORD_BYTES - ADDRESS_BYTES
    if raw[:padding] != b"\x00" * padding:
        raise ChainError(
            f"topic {label} bukan alamat yang di-pad: {padding} byte awalnya bukan nol"
        )
    try:
        return normalize_address("0x" + raw[padding:].hex())
    except AddressError as exc:
        raise ChainError(f"alamat {label} di log tidak sah: {exc}") from None


def _first_line(exc: BaseException) -> str:
    """Baris pertama pesan pengecualian, supaya log tetap satu baris."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__
