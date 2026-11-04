"""Klien chain tiruan dan kunci wallet uji. Tidak ada jaringan di tes mana pun.

Dipakai tes unit dan tes integrasi. Satu-satunya cara tes menyentuh "chain"
adalah lewat :class:`FakeChain`, yang memenuhi protokol
:class:`payment_guard.chain.ChainClient`.

Dua sifat tiruan ini sengaja setia pada yang asli:

* ``send_raw_transaction`` menghitung hash dari byte yang dikirim, yaitu
  ``keccak256(raw_tx)`` — rumus yang sama dengan yang dipakai Ethereum. Jadi
  hash yang dikembalikan tiruan ini benar-benar cocok dengan hash yang
  dihasilkan tanda tangan lokal, dan tes bisa membandingkan keduanya.
* Nonce yang sudah dikonfirmasi dan nonce berikutnya dipisah, seperti
  ``eth_getTransactionCount`` pada tag ``latest`` dan ``pending``. Perbedaan
  itulah yang dipakai recovery untuk memutuskan nonce sudah dipakai orang lain.

Kunci di berkas ini adalah **seed uji publik** (akun pertama Hardhat, muncul
di dokumentasinya dan di ribuan repo). Ia tidak pernah memegang dana nyata dan
tidak dipakai untuk apa pun selain menandatangani transaksi di tes yang tidak
pernah disiarkan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from payment_guard.chain import ChainError, Fees, Receipt
from payment_guard.hashing import keccak256

#: Seed uji publik: akun pertama Hardhat. Bukan dana nyata, bukan rahasia.
TEST_WALLET_SEED: Final = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: Alamat yang diturunkan dari seed di atas, lowercase.
TEST_WALLET_ADDRESS: Final = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

#: Saldo bawaan tiruan: 1 ETH, cukup supaya tidak ada peringatan gas.
DEFAULT_BALANCE_WEI: Final = 10**18

#: Blok bawaan untuk receipt yang dibuat tes.
DEFAULT_BLOCK_NUMBER: Final = 1_000_000


def write_test_wallet(directory: Path, *, name: str = "executor_wallet.json") -> Path:
    """Tulis berkas kunci wallet uji ke ``directory`` dan kembalikan path-nya.

    Pemanggil WAJIB memberi direktori di luar akar repo, biasanya ``tmp_path``:
    :func:`payment_guard.config.load_executor_config` menolak kunci di dalam
    repo, dan tes tidak boleh menguji penolakan itu dengan melanggarnya.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps({"address": TEST_WALLET_ADDRESS, "private_key": TEST_WALLET_SEED}),
        encoding="utf-8",
    )
    return path


def tx_hash_of(raw_tx: bytes) -> str:
    """Hash transaksi dari byte mentahnya: ``keccak256(raw_tx)``."""
    return "0x" + keccak256(bytes(raw_tx)).hex()


@dataclass
class FakeChain:
    """Klien chain dalam memori.

    Setiap sifat yang ingin diuji bisa diatur lewat atribut, dan setiap
    panggilan dicatat di :attr:`calls` supaya tes bisa memeriksa urutannya.
    """

    chain_id_value: int = 84532
    decimals_value: int = 6
    balance_wei_value: int = DEFAULT_BALANCE_WEI
    pending_nonce_value: int = 0
    confirmed_nonce_value: int = 0
    gas_value: int = 60_000
    fees_value: Fees = field(
        default_factory=lambda: Fees(max_fee_per_gas=2_000_000_000, max_priority_fee_per_gas=1)
    )

    #: Hash yang sudah punya receipt.
    receipts: dict[str, Receipt] = field(default_factory=dict)
    #: Byte yang pernah dikirim, dalam urutan pengiriman.
    sent: list[bytes] = field(default_factory=list)
    #: Nama method yang dipanggil, dalam urutan pemanggilan.
    calls: list[str] = field(default_factory=list)

    #: Bila terisi, ``send_raw_transaction`` melempar :class:`ChainError`.
    send_failure: str | None = None
    #: Bila terisi, ``receipt`` melempar :class:`ChainError`.
    receipt_failure: str | None = None

    # -- ChainClient ------------------------------------------------------- #

    def chain_id(self) -> int:
        self.calls.append("chain_id")
        return self.chain_id_value

    def token_decimals(self, token: str) -> int:
        self.calls.append("token_decimals")
        return self.decimals_value

    def eth_balance(self, address: str) -> int:
        self.calls.append("eth_balance")
        return self.balance_wei_value

    def pending_nonce(self, address: str) -> int:
        self.calls.append("pending_nonce")
        return self.pending_nonce_value

    def confirmed_nonce(self, address: str) -> int:
        self.calls.append("confirmed_nonce")
        return self.confirmed_nonce_value

    def fees(self) -> Fees:
        self.calls.append("fees")
        return self.fees_value

    def estimate_gas(self, transaction: object) -> int:
        self.calls.append("estimate_gas")
        return self.gas_value

    def send_raw_transaction(self, raw_tx: bytes) -> str:
        self.calls.append("send_raw_transaction")
        if self.send_failure is not None:
            raise ChainError(self.send_failure)
        self.sent.append(bytes(raw_tx))
        return tx_hash_of(raw_tx)

    def receipt(self, tx_hash: str) -> Receipt | None:
        self.calls.append("receipt")
        if self.receipt_failure is not None:
            raise ChainError(self.receipt_failure)
        return self.receipts.get(tx_hash)

    # -- Pembantu tes ------------------------------------------------------ #

    def confirm(self, tx_hash: str, *, block_number: int = DEFAULT_BLOCK_NUMBER) -> Receipt:
        """Beri ``tx_hash`` receipt yang berhasil."""
        receipt = Receipt(tx_hash=tx_hash, status=1, block_number=block_number)
        self.receipts[tx_hash] = receipt
        return receipt

    def revert(self, tx_hash: str, *, block_number: int = DEFAULT_BLOCK_NUMBER) -> Receipt:
        """Beri ``tx_hash`` receipt yang revert."""
        receipt = Receipt(tx_hash=tx_hash, status=0, block_number=block_number)
        self.receipts[tx_hash] = receipt
        return receipt

    def steal_nonce(self, nonce: int) -> None:
        """Buat seolah transaksi lain sudah memakai ``nonce``.

        Nonce yang sudah dikonfirmasi digeser melewatinya, yang di chain
        sungguhan berarti transaksi kita tidak akan pernah bisa masuk.
        """
        self.confirmed_nonce_value = nonce + 1

    def broadcast_count(self) -> int:
        """Berapa kali byte dikirim. Yang identik tetap dihitung terpisah."""
        return len(self.sent)

    def distinct_broadcasts(self) -> int:
        """Berapa transaksi **berbeda** yang pernah dikirim.

        Inilah angka yang penting untuk idempotensi: menyiarkan byte yang sama
        dua kali tetap satu pembayaran; menyiarkan dua byte berbeda bukan.
        """
        return len({tx_hash_of(raw) for raw in self.sent})
