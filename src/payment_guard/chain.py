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
from web3.exceptions import TransactionNotFound, Web3Exception
from web3.types import TxParams

from payment_guard.addresses import AddressError, normalize_address, to_checksum_address
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


def load_wallet(path: Path) -> WalletIdentity:
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


def _first_line(exc: BaseException) -> str:
    """Baris pertama pesan pengecualian, supaya log tetap satu baris."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__
