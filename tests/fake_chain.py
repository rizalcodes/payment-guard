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

from payment_guard.chain import ChainError, Fees, Receipt, TransferLog
from payment_guard.hashing import keccak256
from payment_guard.money import BaseUnits

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


def fake_hash(label: str) -> str:
    """Hash 32 byte yang bisa diulang, dari ``label``.

    Dipakai untuk hash blok di tes: ia memenuhi CHECK
    ``'^0x[0-9a-f]{64}$'`` dan label yang sama selalu memberi hash yang sama,
    jadi tes bisa menulis ``fake_hash("blok-7")`` dan ``fake_hash("blok-7-v2")``
    untuk membedakan rantai sebelum dan sesudah reorg.
    """
    return "0x" + keccak256(label.encode()).hex()


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

    # -- Keadaan rantai untuk Watcher (M6) --------------------------------- #

    #: Nomor blok terakhir yang "ada" di tiruan ini.
    latest_block_value: int = 0
    #: ``block_number -> block_hash`` kanonik. Nomor di luar peta ini, tapi
    #: masih ``<= latest_block_value``, mendapat hash turunan yang stabil.
    block_hashes: dict[int, str] = field(default_factory=dict)
    #: Semua log ``Transfer`` yang ada di rantai, dalam urutan apa pun.
    #: ``transfer_logs`` yang menyaringnya, sama seperti node.
    logs: list[TransferLog] = field(default_factory=list)
    #: Bila terisi, ``transfer_logs`` melempar :class:`ChainError`.
    logs_failure: str | None = None
    #: Rentang blok yang pernah diminta, untuk memeriksa batch dan kursor.
    log_ranges: list[tuple[int, int]] = field(default_factory=list)

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

    def latest_block(self) -> int:
        self.calls.append("latest_block")
        return self.latest_block_value

    def block_hash(self, block_number: int) -> str | None:
        """Hash kanonik blok itu, atau ``None`` bila belum ada.

        Blok yang tidak disebut di :attr:`block_hashes` mendapat hash turunan
        yang stabil, supaya tes yang tidak peduli hash tidak perlu mengisi
        peta blok sama sekali. Tes reorg mengisinya dengan sengaja.
        """
        self.calls.append("block_hash")
        if block_number > self.latest_block_value or block_number < 0:
            return None
        return self.block_hashes.get(block_number, fake_hash(f"blok-{block_number}"))

    def transfer_logs(
        self,
        token: str,
        *,
        from_addr: str,
        first_block: int,
        last_block: int,
    ) -> tuple[TransferLog, ...]:
        """Saring seperti node: per pengirim dan per rentang blok.

        Penyaringannya ada di sini, bukan di pemanggil, justru karena itu yang
        dilakukan node sungguhan. Tiruan yang mengembalikan semua log akan
        membuat tes lulus walau Watcher lupa memasang filter.
        """
        self.calls.append("transfer_logs")
        if self.logs_failure is not None:
            raise ChainError(self.logs_failure)
        self.log_ranges.append((first_block, last_block))
        return tuple(
            log
            for log in self.logs
            if log.from_addr == from_addr and first_block <= log.block_number <= last_block
        )

    # -- Pembantu tes ------------------------------------------------------ #

    def add_transfer(
        self,
        *,
        tx_hash: str,
        to_addr: str,
        amount_base: int,
        block_number: int,
        log_index: int = 0,
        from_addr: str = TEST_WALLET_ADDRESS,
        block_hash: str | None = None,
        confirm: bool = True,
    ) -> TransferLog:
        """Taruh satu log ``Transfer`` di rantai tiruan.

        Secara bawaan ia juga memberi transaksinya receipt dan memajukan
        ``latest_block_value`` sampai blok itu: log yang ada di blok yang
        "belum terjadi" adalah keadaan yang tidak mungkin di chain, dan tes
        yang membuatnya akan menguji hal yang tidak pernah terjadi.
        """
        resolved_hash = block_hash or fake_hash(f"blok-{block_number}")
        self.block_hashes.setdefault(block_number, resolved_hash)
        self.latest_block_value = max(self.latest_block_value, block_number)
        log = TransferLog(
            chain_id=self.chain_id_value,
            tx_hash=tx_hash,
            log_index=log_index,
            block_number=block_number,
            block_hash=resolved_hash,
            from_addr=from_addr,
            to_addr=to_addr,
            amount_base=BaseUnits(amount_base),
        )
        self.logs.append(log)
        if confirm:
            self.receipts[tx_hash] = Receipt(tx_hash=tx_hash, status=1, block_number=block_number)
        return log

    def advance_to(self, block_number: int) -> None:
        """Majukan kepala rantai ke ``block_number``."""
        self.latest_block_value = max(self.latest_block_value, block_number)

    def reorg_block(self, block_number: int, *, label: str | None = None) -> str:
        """Ganti hash kanonik blok ``block_number``: rantainya berubah.

        Receipt dan log TIDAK diubah di sini. Itu disengaja: reorg yang
        memindahkan transaksi dan reorg yang menghilangkannya adalah dua
        keadaan berbeda di PRD v2 bagian 10, dan tes harus bisa membuat
        keduanya. Pakai :meth:`drop_transaction` atau :meth:`move_transaction`
        untuk menentukan yang mana.
        """
        new_hash = fake_hash(label or f"blok-{block_number}-setelah-reorg")
        self.block_hashes[block_number] = new_hash
        return new_hash

    def drop_transaction(self, tx_hash: str) -> None:
        """Hilangkan receipt-nya: transaksinya tidak ada di rantai baru."""
        self.receipts.pop(tx_hash, None)
        self.logs = [log for log in self.logs if log.tx_hash != tx_hash]

    def move_transaction(self, tx_hash: str, *, to_block: int) -> None:
        """Pindahkan transaksinya ke blok lain, seperti reorg yang menata ulang."""
        self.block_hashes.setdefault(to_block, fake_hash(f"blok-{to_block}"))
        self.latest_block_value = max(self.latest_block_value, to_block)
        self.receipts[tx_hash] = Receipt(tx_hash=tx_hash, status=1, block_number=to_block)
        self.logs = [
            TransferLog(
                chain_id=log.chain_id,
                tx_hash=log.tx_hash,
                log_index=log.log_index,
                block_number=to_block,
                block_hash=self.block_hashes[to_block],
                from_addr=log.from_addr,
                to_addr=log.to_addr,
                amount_base=log.amount_base,
            )
            if log.tx_hash == tx_hash
            else log
            for log in self.logs
        ]

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
