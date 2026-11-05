"""Watcher: log ``Transfer`` USDC dari wallet Executor menjadi ``settlements``.

PRD v2 bagian 10, PRD v2.1 bagian 10, Arsitektur v1.1 bagian 10.

Watcher adalah satu-satunya penulis ``settlements`` dan ``watcher_state``, dan
ia **tidak** menulis apa pun selain itu (plus ``audit_log``). Ia tidak punya
hak baca sama sekali pada ``decisions`` maupun ``executions``
(``0002_roles.sql``, matriks di :mod:`payment_guard.privileges`), dan itu
disengaja: pencocokan antara apa yang terjadi di chain dan keputusan yang
mengizinkannya adalah pekerjaan :mod:`payment_guard.reconciler`, yang punya
SELECT pada ketiganya. Proses yang bisa membaca keduanya sekaligus adalah
proses yang bisa, suatu hari, "memperbaiki" salah satunya.

Tiga hal yang membuat modul ini bisa dipercaya:

1. **Yang dipantau adalah pengirim, bukan penerima.** Filternya
   ``from = alamat wallet Executor`` (PRD v2 bagian 10). Menyaring per penerima
   akan membuat transfer dari wallet kami ke tujuan yang tidak kami kenal tidak
   pernah terlihat — padahal justru itu temuan yang paling penting
   (``unmatched_onchain``).
2. **Tidak ada transfer yang diabaikan diam-diam.** Setiap log yang lolos
   filter menjadi satu baris ``settlements``. Baris yang tidak punya padanan di
   ``executions`` tetap ada, dan Reconciler yang menamainya.
3. **``final`` berarti sesuatu.** Baris hanya menjadi ``final`` setelah
   melewati kedalaman konfirmasi **dan** hash bloknya masih kanonik, dan baris
   yang sudah ``final`` tetap diperiksa ulang di dalam jendela mundur K.

**Pemindaian berhenti di kepala aman** (``latest - CONFIRMATIONS``), bukan di
kepala rantai. Konsekuensinya perlu diketahui: setiap baris yang baru ditulis
sudah memenuhi syarat ``final`` pada pemeriksaan **berikutnya**, jadi
``pending`` bertahan tepat satu siklus — selebar satu jeda
``WATCHER_INTERVAL_SECONDS``.

Itu pilihan yang disengaja. Memindai sampai kepala rantai akan membuat
pembayaran terlihat beberapa detik lebih cepat, tapi juga membuat kami mencatat
baris dari blok yang belum tentu bertahan, lalu harus menanganinya mundur. Yang
tidak dipindai tidak perlu dibatalkan. Kursor juga tidak pernah maju melewati
kepala aman, jadi blok yang belum cukup dalam akan dipindai nanti, bukan
hilang.

Transisi ``pending -> pending`` (dipindah) dan ``pending -> reorged`` tetap
terjangkau: lewat jendela satu siklus itu, dan lewat ``reorged -> pending``
yang mengembalikan baris ke ``pending`` kapan pun transaksinya muncul lagi.

Semua penilaian waktu memakai jam **database** (``now()`` di SQL), bukan jam
proses: Arsitektur v1.1 prinsip 5.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, TextIO

import psycopg

from payment_guard.chain import (
    ChainClient,
    ChainError,
    TransferLog,
    Web3Client,
    check_chain_and_token,
)
from payment_guard.config import SCAN_RANGE_FLOOR, WatcherConfig
from payment_guard.db import connect, open_connection, to_int, try_session_lock
from payment_guard.events import Actor, Event, audit
from payment_guard.money import format_base_units

# --------------------------------------------------------------------------- #
# Status settlement
# --------------------------------------------------------------------------- #

#: Baris sudah terlihat di chain, belum melewati kedalaman konfirmasi.
STATUS_PENDING: Final = "pending"
#: Melewati kedalaman konfirmasi, dan hash bloknya masih kanonik saat diperiksa.
STATUS_FINAL: Final = "final"
#: Blok berubah dan transaksinya tidak ada lagi. Barisnya **tidak** dihapus.
STATUS_REORGED: Final = "reorged"

#: Semua status yang sah. Sama dengan CHECK di ``0001_init.sql``.
SETTLEMENT_STATUSES: Final[tuple[str, ...]] = (STATUS_PENDING, STATUS_FINAL, STATUS_REORGED)

#: Status yang masih bisa berubah dan karena itu diperiksa ulang tiap siklus.
OPEN_STATUSES: Final[tuple[str, ...]] = (STATUS_PENDING, STATUS_REORGED)

#: Potongan pesan error RPC yang berarti "rentang bloknya terlalu lebar".
#:
#: Tidak ada kode error standar untuk ini. Setiap penyedia menuliskannya
#: sendiri — "query returned more than 10000 results", "block range too large",
#: "exceeds the limit" — jadi yang bisa dilakukan adalah mencocokkan kata, dan
#: itu ditulis di sini apa adanya alih-alih disembunyikan di tengah fungsi.
#:
#: Daftar ini **melonggarkan** penanganan error, jadi arah kesalahannya
#: penting: kata yang terlalu umum akan membuat kegagalan lain disalahartikan
#: sebagai soal rentang dan dicoba ulang dengan rentang lebih kecil, yang
#: akhirnya tetap gagal — hanya lebih lambat dan dengan pesan yang
#: menyesatkan. Karena itu daftarnya pendek, dan semua yang tidak cocok
#: dilempar apa adanya.
RANGE_REJECTION_MARKERS: Final[tuple[str, ...]] = (
    "range",
    "too large",
    "more than",
    "limit",
)

#: Nilai ``last_scanned`` saat belum ada blok yang dipindai.
#:
#: Tidak pernah disimpan: kolomnya ``CHECK (last_scanned >= 0)``, dan keadaan
#: "belum pernah memindai" diwakili oleh **tidak adanya baris** di
#: ``watcher_state``. Konstanta ini hanya dipakai di aritmetika, supaya
#: ``last_scanned + 1`` berlaku seragam untuk kursor yang ada maupun tidak —
#: pola yang sama dengan ``NO_NONCE_USED`` di :mod:`payment_guard.executor`,
#: dan karena alasan yang sama: blok 0 adalah blok yang sah.
NO_BLOCK_SCANNED: Final = -1


# --------------------------------------------------------------------------- #
# SQL. Statik; nilai selalu lewat parameter.
# --------------------------------------------------------------------------- #

SELECT_CURSOR: Final = """
SELECT last_scanned FROM watcher_state WHERE chain_id = %s
"""

#: Satu pernyataan untuk membuat dan memajukan kursor.
#:
#: ``ON CONFLICT ... DO UPDATE`` memberi keduanya tanpa cabang "sudah ada atau
#: belum" di Python, dan tanpa jendela antara SELECT dan INSERT. Watcher
#: memegang lock sesi per chain, jadi tidak ada penulis kedua — tapi kursor
#: yang maju lewat dua jalur kode berbeda tetap punya dua peluang untuk salah.
UPSERT_CURSOR: Final = """
INSERT INTO watcher_state (chain_id, last_scanned) VALUES (%s, %s)
ON CONFLICT (chain_id) DO UPDATE SET last_scanned = EXCLUDED.last_scanned
"""

#: ``DO NOTHING`` adalah inti idempotensinya (PRD v2 bagian 10 nomor 2).
#:
#: Memproses rentang blok yang sama dua kali tidak menghasilkan baris kedua,
#: dan yang menjaminnya bukan pemeriksaan di Python melainkan
#: ``UNIQUE (chain_id, tx_hash, log_index)``. ``DO NOTHING`` juga berarti baris
#: yang sudah ``final`` tidak akan dikembalikan ke ``pending`` oleh pemindaian
#: ulang: nilai yang dimasukkan diabaikan seluruhnya, bukan digabung.
INSERT_SETTLEMENT: Final = """
INSERT INTO settlements (
  chain_id, tx_hash, log_index, block_number, block_hash,
  from_addr, to_addr, amount_base, status
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (chain_id, tx_hash, log_index) DO NOTHING
"""

SELECT_OPEN_SETTLEMENTS: Final = """
SELECT id, tx_hash, log_index, block_number, block_hash, status
FROM settlements
WHERE chain_id = %s AND status = ANY(%s)
ORDER BY block_number, log_index
"""

#: Baris ``final`` di dalam jendela mundur K, untuk diperiksa ulang.
#:
#: Hanya jendela itu, bukan seluruh tabel: baris yang jauh di belakang tidak
#: akan ter-reorg lagi, dan memeriksanya setiap siklus berarti satu panggilan
#: RPC per baris selamanya.
SELECT_FINAL_IN_WINDOW: Final = """
SELECT id, tx_hash, log_index, block_number, block_hash, status
FROM settlements
WHERE chain_id = %s AND status = %s AND block_number >= %s
ORDER BY block_number, log_index
"""

#: ``now()`` adalah jam database. Lihat Arsitektur v1.1 prinsip 5.
MARK_FINAL: Final = """
UPDATE settlements
SET status = %s, finalized_at = now(), last_verified_at = now()
WHERE id = %s AND status = %s
"""

MARK_VERIFIED: Final = """
UPDATE settlements SET last_verified_at = now() WHERE id = %s
"""

MOVE_BLOCK: Final = """
UPDATE settlements
SET block_number = %s, block_hash = %s, status = %s, last_verified_at = now()
WHERE id = %s
"""

MARK_REORGED: Final = """
UPDATE settlements SET status = %s, last_verified_at = now() WHERE id = %s
"""


class WatcherError(RuntimeError):
    """Kegagalan di jalur Watcher yang bukan kegagalan RPC.

    Pesannya ditampilkan ke operator, jadi ia tidak pernah memuat URL database
    maupun URL RPC utuh.
    """


# --------------------------------------------------------------------------- #
# Baris yang dibaca ulang
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SettlementRow:
    """Satu baris ``settlements`` sejauh yang dibutuhkan state machine."""

    id: int
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    status: str


@dataclass(frozen=True)
class ScanReport:
    """Hasil satu siklus Watcher. Dipakai CLI dan tes, bukan dicetak mentah."""

    chain_id: int
    latest_block: int
    safe_head: int
    first_block: int
    last_block: int
    logs_seen: int
    settlements_inserted: int
    finalized: int
    moved: int
    reorged: int
    restored: int
    alarms: int
    #: Berapa ``eth_getLogs`` dipakai untuk rentang siklus ini. Lebih dari satu
    #: berarti node menolak lebarnya dan rentangnya dipecah.
    ranges_fetched: int = 0
    #: Berapa kali rentang dibagi dua karena ditolak node.
    range_shrinks: int = 0
    cursor_rewound_to: int | None = None
    scanned: bool = True
    #: ``--from-block`` diberikan padahal kursor sudah ada, jadi diabaikan.
    from_block_ignored: int | None = None

    def render(self) -> str:
        """Ringkasan satu siklus, satu baris per hal yang berubah."""
        awal = []
        if self.from_block_ignored is not None:
            awal.append(
                f"PERINGATAN: --from-block {self.from_block_ignored} DIABAIKAN. "
                "Kursor watcher_state sudah ada, dan ia tidak pernah dipindahkan ke "
                "depan oleh flag: itu akan melewatkan blok dalam diam. Lihat "
                "docs/runbook.md bagian 8."
            )
        if not self.scanned:
            return "\n".join(
                [
                    *awal,
                    f"blok terakhir {self.latest_block}, kedalaman konfirmasi belum "
                    "terlampaui; tidak ada yang dipindai",
                ]
            )
        lines = [
            *awal,
            f"blok {self.first_block}..{self.last_block} "
            f"(aman sampai {self.safe_head}, terakhir {self.latest_block})",
            f"  transfer terlihat {self.logs_seen}, settlement baru {self.settlements_inserted}",
        ]
        for label, count in (
            ("final", self.finalized),
            ("dipindah", self.moved),
            ("ter-reorg", self.reorged),
            ("kembali", self.restored),
        ):
            if count:
                lines.append(f"  {label}: {count}")
        if self.ranges_fetched > 1:
            lines.append(
                f"  rentang dipecah menjadi {self.ranges_fetched} permintaan "
                f"({self.range_shrinks} kali dibagi dua karena ditolak node)"
            )
        if self.alarms:
            lines.append(f"  ALARM: {self.alarms} baris final ter-reorg")
        if self.cursor_rewound_to is not None:
            lines.append(f"  kursor dimundurkan ke {self.cursor_rewound_to}")
        return "\n".join(lines)


@dataclass
class WatcherRuntime:
    """Konfigurasi, klien chain, dan koneksi pemegang lock sesi.

    Bukan ``frozen``: ``conn`` ditutup oleh :func:`close_runtime`, dan lock
    sesinya hidup selama koneksi itu hidup.
    """

    config: WatcherConfig
    client: ChainClient
    conn: psycopg.Connection
    counters: dict[str, int] = field(default_factory=dict)


def open_runtime(
    config: WatcherConfig,
    *,
    client: ChainClient | None = None,
) -> WatcherRuntime:
    """Sambungkan ke database, ambil lock sesi ``watcher:<chain_id>``.

    Lock-nya **sesi**, bukan transaksi: dua Watcher untuk chain yang sama tidak
    boleh jalan bersamaan sama sekali (Arsitektur v1.1 bagian 10), bukan hanya
    tidak boleh menulis bersamaan. ``pg_try_advisory_lock`` dipakai, bukan
    ``pg_advisory_lock``: Watcher kedua harus **gagal dan mengatakannya**,
    bukan menunggu diam-diam sampai yang pertama berhenti.

    Raises:
        WatcherError: bila lock tidak bisa diambil.
    """
    conn = open_connection(config.db_url, lock_timeout_ms=config.lock_timeout_ms)
    try:
        with conn.cursor() as cur:
            if not try_session_lock(cur, "watcher", str(config.chain_id)):
                raise WatcherError(
                    f"Watcher lain sudah memegang lock untuk chain {config.chain_id}. "
                    "Hanya satu Watcher per chain; hentikan yang lain lebih dulu."
                )
        conn.commit()
    except BaseException:
        conn.close()
        raise

    return WatcherRuntime(
        config=config,
        client=Web3Client(config.rpc_url) if client is None else client,
        conn=conn,
    )


def close_runtime(runtime: WatcherRuntime) -> None:
    """Tutup koneksi, yang melepas lock sesinya."""
    runtime.conn.close()


def start_watcher(
    config: WatcherConfig,
    *,
    client: ChainClient | None = None,
    out: TextIO | None = None,
) -> WatcherRuntime:
    """Buka runtime lalu periksa chain id dan ``decimals()`` sebelum bekerja.

    Keduanya menggagalkan start (Arsitektur v1.1 bagian 10): chain yang salah
    berarti setiap hash yang kami simpan menunjuk jaringan lain, dan
    ``decimals()`` yang bukan 6 berarti setiap jumlah yang kami catat punya
    arti lain. Saldo gas **tidak** diperiksa: Watcher tidak mengirim apa pun.

    Raises:
        ChainError: bila chain id atau ``decimals()`` tidak cocok.
        WatcherError: bila lock sesi tidak bisa diambil.
    """
    runtime = open_runtime(config, client=client)
    try:
        chain_id, decimals = check_chain_and_token(
            runtime.client,
            expected_chain_id=config.chain_id,
            usdc_address=config.usdc_address,
        )
    except BaseException:
        close_runtime(runtime)
        raise
    if out is not None:
        print(
            f"watcher siap: chain {chain_id}, USDC {config.usdc_address} "
            f"({decimals} desimal), memantau transfer dari {config.wallet_address}",
            file=out,
        )
    return runtime


# --------------------------------------------------------------------------- #
# Kursor
# --------------------------------------------------------------------------- #


def read_cursor(cur: psycopg.Cursor, chain_id: int) -> int:
    """``last_scanned`` untuk ``chain_id``, atau :data:`NO_BLOCK_SCANNED`.

    Tidak adanya baris berarti belum ada blok yang dipindai, dan itu **tidak**
    sama dengan ``last_scanned = 0``: blok 0 adalah blok yang sah, dan
    menganggapnya sudah dipindai akan melewatkannya selamanya.
    """
    cur.execute(SELECT_CURSOR, (chain_id,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return NO_BLOCK_SCANNED
    return to_int(row[0])


def write_cursor(cur: psycopg.Cursor, chain_id: int, last_scanned: int) -> None:
    """Setel ``last_scanned``, naik maupun turun.

    Turun adalah jalur reorg: blok yang sudah kami pindai ternyata berubah,
    jadi kami memindainya lagi. Karena itu fungsi ini tidak menolak nilai yang
    lebih kecil — yang menolak baris ganda adalah kunci unik di
    ``settlements``, bukan arah kursor.
    """
    if last_scanned < 0:
        raise WatcherError(
            f"last_scanned tidak boleh negatif, dapat {last_scanned}; "
            "keadaan 'belum pernah memindai' diwakili oleh tidak adanya baris"
        )
    cur.execute(UPSERT_CURSOR, (chain_id, last_scanned))


# --------------------------------------------------------------------------- #
# Satu siklus
# --------------------------------------------------------------------------- #


def scan_once(
    runtime: WatcherRuntime,
    *,
    from_block: int | None = None,
    out: TextIO | None = None,
) -> ScanReport:
    """Satu siklus: periksa ulang yang terbuka, lalu pindai rentang berikutnya.

    Urutannya disengaja. Pemeriksaan ulang lebih dulu, karena ia bisa
    **memundurkan** kursor: blok yang hash-nya berubah harus dipindai lagi, dan
    memindai ke depan sebelum tahu itu berarti satu siklus terbuang. Setelah
    kursor dimundurkan, pemindaian di siklus yang sama langsung mengerjakan
    rentang yang benar.

    ``from_block`` hanya berlaku ketika **belum ada** kursor, dan ia hanya bisa
    melompati sejarah dengan sengaja — lihat :func:`_initial_block`.

    Args:
        from_block: blok awal untuk pemindaian pertama kali.
        out: tempat ringkasan dicetak. ``None`` berarti tidak mencetak.

    Raises:
        ChainError: bila RPC gagal setelah seluruh percobaan.
        WatcherError: bila keadaan database tidak masuk akal.
    """
    config = runtime.config
    latest = runtime.client.latest_block()
    safe_head = latest - config.confirmations

    verdicts = _verify_open(runtime, latest=latest, safe_head=safe_head)
    rewound = _apply_rewind(runtime, verdicts)

    # Kursor dibaca **setelah** pemeriksaan ulang, karena pemeriksaan itu bisa
    # memundurkannya. Dibaca sebelum kedua cabang di bawah supaya peringatan
    # `--from-block` muncul walau tidak ada yang dipindai siklus ini: operator
    # yang salah memakai flag itu harus diberi tahu sekarang, bukan nanti.
    with runtime.conn.cursor() as cur:
        cursor_value = read_cursor(cur, config.chain_id)
    ignored = from_block if (cursor_value != NO_BLOCK_SCANNED and from_block is not None) else None

    if safe_head < 0:
        report = ScanReport(
            chain_id=config.chain_id,
            latest_block=latest,
            safe_head=safe_head,
            first_block=0,
            last_block=0,
            logs_seen=0,
            settlements_inserted=0,
            finalized=verdicts.finalized,
            moved=verdicts.moved,
            reorged=verdicts.reorged,
            restored=verdicts.restored,
            alarms=verdicts.alarms,
            cursor_rewound_to=rewound,
            scanned=False,
            from_block_ignored=ignored,
        )
        if out is not None:
            print(report.render(), file=out)
        return report

    first_block = _initial_block(cursor_value, from_block=from_block, safe_head=safe_head)
    last_block = min(first_block + config.scan_batch_size - 1, safe_head)

    if first_block > safe_head:
        # Tidak ada blok baru yang sudah aman. Bukan kegagalan, dan kursor
        # tidak disentuh: memajukannya ke safe_head akan menandai blok yang
        # belum dipindai sebagai sudah dipindai.
        report = ScanReport(
            chain_id=config.chain_id,
            latest_block=latest,
            safe_head=safe_head,
            first_block=first_block,
            last_block=safe_head,
            logs_seen=0,
            settlements_inserted=0,
            finalized=verdicts.finalized,
            moved=verdicts.moved,
            reorged=verdicts.reorged,
            restored=verdicts.restored,
            alarms=verdicts.alarms,
            cursor_rewound_to=rewound,
            from_block_ignored=ignored,
        )
        if out is not None:
            print(report.render(), file=out)
        return report

    seen, inserted, ranges, shrinks = _scan_range(
        runtime, first_block=first_block, last_block=last_block
    )

    report = ScanReport(
        chain_id=config.chain_id,
        latest_block=latest,
        safe_head=safe_head,
        first_block=first_block,
        last_block=last_block,
        logs_seen=seen,
        settlements_inserted=inserted,
        ranges_fetched=ranges,
        range_shrinks=shrinks,
        finalized=verdicts.finalized,
        moved=verdicts.moved,
        reorged=verdicts.reorged,
        restored=verdicts.restored,
        alarms=verdicts.alarms,
        cursor_rewound_to=rewound,
        from_block_ignored=ignored,
    )
    if out is not None:
        print(report.render(), file=out)
    return report


def _initial_block(cursor_value: int, *, from_block: int | None, safe_head: int) -> int:
    """Blok pertama yang dipindai siklus ini.

    Dengan kursor yang sudah ada, jawabannya selalu ``last_scanned + 1``, dan
    ``from_block`` **diabaikan**: memindahkan kursor yang sudah berjalan ke
    depan berarti melewatkan blok dalam diam, dan itu adalah satu-satunya
    kesalahan yang tidak bisa diperbaiki belakangan oleh modul ini. Peringatan
    dicetak oleh :func:`scan_once`, yang punya aliran keluaran.

    Tanpa kursor, ``from_block`` **wajib**. Tidak ada default.

    M6 sempat memakai 0 sebagai default, dengan alasan bahwa memindai dari
    genesis lambat tapi tidak pernah melewatkan apa pun. Itu salah untuk Base
    Sepolia: dari genesis sampai kepala rantai ada puluhan juta blok, jadi
    kursornya praktis tidak pernah sampai, dan Watcher yang terlihat jalan tapi
    tidak akan pernah mencapai pembayaran hari ini lebih buruk daripada Watcher
    yang menolak start. Angka yang benar hanya diketahui operator — blok di
    sekitar funding wallet Executor — jadi ia yang menyebutkannya, sekali,
    dengan sengaja. Prosedurnya di ``docs/runbook.md`` bagian 8.

    Raises:
        WatcherError: bila belum ada kursor dan ``from_block`` tidak diberikan,
            atau bila ``from_block`` negatif.
    """
    if cursor_value != NO_BLOCK_SCANNED:
        return cursor_value + 1
    if from_block is None:
        raise WatcherError(
            "belum ada kursor di watcher_state untuk chain ini, jadi Watcher tidak tahu "
            "dari blok mana harus mulai. Jalankan sekali dengan `guard watch --from-block "
            "<nomor blok>`; pakai blok di sekitar funding wallet Executor dikurangi margin "
            "kecil, bukan 0. Lihat docs/runbook.md bagian 8."
        )
    if from_block < 0:
        raise WatcherError(f"--from-block tidak boleh negatif, dapat {from_block}")
    return min(from_block, safe_head + 1)


def _scan_range(
    runtime: WatcherRuntime,
    *,
    first_block: int,
    last_block: int,
) -> tuple[int, int, int, int]:
    """Pindai ``first_block..last_block``, sekecil apa pun potongan yang diterima node.

    Mengembalikan ``(log_terlihat, baris_baru, jumlah_rentang, jumlah_pengecilan)``.

    Satu rentang = satu ``eth_getLogs`` + satu transaksi database yang menyimpan
    hasilnya **dan** memajukan kursor. Aturan batch M6 tidak berubah; yang
    berubah hanyalah bahwa satu siklus bisa berisi beberapa batch, karena
    rentang yang diminta bisa harus dipecah.

    Konsekuensinya penting dan disengaja: kalau rentang ketiga gagal, dua
    rentang pertama tetap tersimpan dan kursornya tetap maju sampai akhir
    rentang kedua. Tidak ada blok yang terlewat, dan tidak ada blok yang
    ditandai sudah dipindai padahal log-nya belum tersimpan.
    """
    seen = 0
    inserted = 0
    ranges = 0
    shrinks = 0
    cursor_at = first_block
    while cursor_at <= last_block:
        logs, fetched_to, used_shrinks = _fetch_one_range(
            runtime, first_block=cursor_at, last_block=last_block
        )
        inserted += _store_batch(runtime, logs, last_block=fetched_to)
        seen += len(logs)
        ranges += 1
        shrinks += used_shrinks
        cursor_at = fetched_to + 1
    return seen, inserted, ranges, shrinks


def _fetch_one_range(
    runtime: WatcherRuntime,
    *,
    first_block: int,
    last_block: int,
) -> tuple[tuple[TransferLog, ...], int, int]:
    """Satu ``eth_getLogs``, mengecil separuh setiap kali node menolak lebarnya.

    Mengembalikan ``(log, blok_terakhir_yang_benar_benar_diambil, jumlah_pengecilan)``.
    Blok terakhir bisa lebih kecil dari ``last_block``: itulah yang membuat
    pemanggil tahu sampai mana kursor boleh maju.

    Yang diulang **hanya** penolakan tentang lebar rentang
    (:data:`RANGE_REJECTION_MARKERS`). Kegagalan lain dilempar apa adanya, tanpa
    diubah menjadi percobaan ulang: node yang menolak karena filter salah, atau
    yang mengembalikan log yang tidak bisa didekode, akan menolak hal yang sama
    dengan rentang separuh — hanya lebih lambat, dan dengan pesan akhir yang
    menyalahkan lebar rentang untuk masalah yang bukan itu.

    Jeda dan batas percobaan RPC tingkat transport sudah ditangani
    :class:`payment_guard.chain.Web3Client`; yang di sini adalah lapis di
    atasnya, tentang **bentuk permintaan**, bukan tentang jaringan.

    Raises:
        ChainError: untuk kegagalan RPC yang bukan soal lebar rentang.
        WatcherError: bila lebarnya sudah di lantai dan masih ditolak, atau
            bila batas percobaan tercapai.
    """
    width = last_block - first_block + 1
    shrinks = 0
    terakhir = ""
    for _ in range(runtime.config.scan_max_attempts):
        end = first_block + width - 1
        try:
            logs = runtime.client.transfer_logs(
                runtime.config.usdc_address,
                from_addr=runtime.config.wallet_address,
                first_block=first_block,
                last_block=end,
            )
        except ChainError as exc:
            if not _is_range_rejection(str(exc)):
                raise
            terakhir = str(exc)
            if width <= SCAN_RANGE_FLOOR:
                raise WatcherError(
                    f"node menolak rentang {first_block}..{end} yang lebarnya sudah "
                    f"{width} blok, yaitu lantainya ({SCAN_RANGE_FLOOR}). Di lebar ini "
                    "penolakan hampir pasti bukan soal rentang; periksa pesan node: "
                    f"{terakhir}"
                ) from None
            width = max(SCAN_RANGE_FLOOR, width // 2)
            shrinks += 1
            continue
        return logs, end, shrinks
    raise WatcherError(
        f"rentang {first_block}..{last_block} gagal setelah "
        f"{runtime.config.scan_max_attempts} percobaan; lebar terakhir yang dicoba "
        f"{width} blok. Kursor tidak dimajukan. Pesan terakhir dari node: {terakhir}"
    )


def _is_range_rejection(message: str) -> bool:
    """Apakah pesan error RPC itu tentang lebar rentang.

    Pencocokan kata, karena tidak ada kode error standar untuk ini. Lihat
    :data:`RANGE_REJECTION_MARKERS`.
    """
    lowered = message.lower()
    return any(marker in lowered for marker in RANGE_REJECTION_MARKERS)


def _store_batch(
    runtime: WatcherRuntime,
    logs: Sequence[TransferLog],
    *,
    last_block: int,
) -> int:
    """Tulis satu batch dan majukan kursor, dalam **satu** transaksi.

    PRD v2 bagian 10 nomor 3: ``last_scanned`` hanya maju setelah batch
    tersimpan, di dalam transaksi yang sama. Crash di tengah batch karena itu
    tidak bisa meninggalkan kursor yang sudah maju melewati log yang belum
    tersimpan — keadaan yang akan melewatkan pembayaran secara permanen.

    Mengembalikan jumlah baris yang benar-benar baru. Selisihnya dari
    ``len(logs)`` adalah baris yang sudah ada, yaitu idempotensinya bekerja.
    """
    config = runtime.config
    inserted = 0
    with runtime.conn.cursor() as cur:
        for log in logs:
            if log.from_addr != config.wallet_address:
                # Node menjawab di luar filter yang diminta. Ini tidak boleh
                # terjadi, dan karena itu tidak boleh dibiarkan: baris dari
                # wallet lain di `settlements` akan membuat rekonsiliasi
                # melaporkan pembayaran yang bukan milik sistem ini.
                raise WatcherError(
                    f"log Transfer dari {log.from_addr}, bukan dari "
                    f"{config.wallet_address}; filter eth_getLogs tidak dipatuhi node"
                )
            if log.chain_id != config.chain_id:
                raise WatcherError(f"log dari chain {log.chain_id}, bukan {config.chain_id}")
            cur.execute(
                INSERT_SETTLEMENT,
                (
                    log.chain_id,
                    log.tx_hash,
                    log.log_index,
                    log.block_number,
                    log.block_hash,
                    log.from_addr,
                    log.to_addr,
                    # `str` supaya NUMERIC(78,0) menerimanya tanpa lewat float;
                    # pola yang sama dengan Executor.
                    format_base_units(log.amount_base),
                    STATUS_PENDING,
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
        write_cursor(cur, config.chain_id, last_block)
    runtime.conn.commit()
    return inserted


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


@dataclass
class VerifyVerdicts:
    """Apa yang berubah di satu pemeriksaan ulang, dan blok yang goyah."""

    finalized: int = 0
    moved: int = 0
    reorged: int = 0
    restored: int = 0
    alarms: int = 0
    shaken_blocks: list[int] = field(default_factory=list)


def _verify_open(runtime: WatcherRuntime, *, latest: int, safe_head: int) -> VerifyVerdicts:
    """Jalankan state machine PRD v2 bagian 10 atas baris yang masih bisa berubah.

    Tiga himpunan baris diperiksa:

    * ``pending`` — bisa menjadi ``final``, dipindah, atau ``reorged``.
    * ``reorged`` — bisa kembali menjadi ``pending`` bila transaksinya muncul
      lagi. Barisnya tidak pernah dihapus, jadi jalan kembali itu selalu ada.
    * ``final`` di dalam jendela mundur K — bisa menjadi ``reorged``, dan itu
      satu-satunya alarm di modul ini.

    Setiap perubahan dan audit-nya berada dalam satu transaksi per baris: baris
    yang statusnya berubah tanpa jejak di ``audit_log``, atau sebaliknya, lebih
    buruk daripada perubahan yang gagal seluruhnya.
    """
    verdicts = VerifyVerdicts()
    config = runtime.config

    with runtime.conn.cursor() as cur:
        cur.execute(SELECT_OPEN_SETTLEMENTS, (config.chain_id, list(OPEN_STATUSES)))
        open_rows = [_row_of(row) for row in cur.fetchall()]
        window_floor = max(0, safe_head - config.reorg_lookback)
        cur.execute(SELECT_FINAL_IN_WINDOW, (config.chain_id, STATUS_FINAL, window_floor))
        final_rows = [_row_of(row) for row in cur.fetchall()]
    runtime.conn.rollback()

    for row in open_rows:
        if row.status == STATUS_PENDING:
            _verify_pending(runtime, row, latest=latest, verdicts=verdicts)
        else:
            _verify_reorged(runtime, row, verdicts=verdicts)

    for row in final_rows:
        _verify_final(runtime, row, verdicts=verdicts)

    return verdicts


def _verify_pending(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    latest: int,
    verdicts: VerifyVerdicts,
) -> None:
    canonical = runtime.client.block_hash(row.block_number)
    if canonical == row.block_hash:
        if latest - row.block_number >= runtime.config.confirmations:
            _finalize(runtime, row)
            verdicts.finalized += 1
        else:
            # Di bawah kedalaman konfirmasi: tetap `pending`. Yang dicatat
            # hanya bahwa ia baru saja diperiksa, supaya operator bisa
            # membedakan "belum dalam" dari "tidak pernah dilihat lagi".
            _touch(runtime, row)
        return
    _handle_hash_change(runtime, row, verdicts=verdicts, from_final=False)


def _verify_reorged(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    verdicts: VerifyVerdicts,
) -> None:
    """``reorged -> pending`` bila transaksinya muncul lagi di blok lain."""
    receipt = runtime.client.receipt(row.tx_hash)
    if receipt is None:
        _touch(runtime, row)
        return
    new_hash = runtime.client.block_hash(receipt.block_number)
    if new_hash is None:
        _touch(runtime, row)
        return
    _move(
        runtime,
        row,
        block_number=receipt.block_number,
        block_hash=new_hash,
        status=STATUS_PENDING,
        event=Event.SETTLEMENT_RESTORED,
    )
    verdicts.restored += 1
    verdicts.shaken_blocks.append(receipt.block_number)


def _verify_final(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    verdicts: VerifyVerdicts,
) -> None:
    """``final -> reorged``: janji yang dibatalkan, dan karena itu sebuah alarm."""
    canonical = runtime.client.block_hash(row.block_number)
    if canonical == row.block_hash:
        _touch(runtime, row)
        return
    with runtime.conn.cursor() as cur:
        cur.execute(MARK_REORGED, (STATUS_REORGED, row.id))
        audit(
            cur,
            Event.ALARM_FINAL_SETTLEMENT_REORGED,
            _payload(row, extra={"previous_status": STATUS_FINAL}),
            actor=Actor.WATCHER,
        )
    runtime.conn.commit()
    verdicts.alarms += 1
    verdicts.reorged += 1
    verdicts.shaken_blocks.append(row.block_number)


def _handle_hash_change(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    verdicts: VerifyVerdicts,
    from_final: bool,
) -> None:
    """Hash blok tidak lagi kanonik: dipindah bila transaksinya masih ada.

    Urutan pemeriksaannya penting. Hash yang berubah **sendirian** tidak
    berarti pembayarannya hilang: transaksi yang sama sering mendarat lagi di
    blok lain. Yang menentukan adalah ada atau tidaknya receipt, dan karena itu
    receipt ditanyakan sebelum apa pun ditandai ``reorged``.
    """
    receipt = runtime.client.receipt(row.tx_hash)
    if receipt is not None:
        new_hash = runtime.client.block_hash(receipt.block_number)
        if new_hash is not None:
            _move(
                runtime,
                row,
                block_number=receipt.block_number,
                block_hash=new_hash,
                status=STATUS_PENDING,
                event=Event.SETTLEMENT_MOVED,
            )
            verdicts.moved += 1
            verdicts.shaken_blocks.append(row.block_number)
            verdicts.shaken_blocks.append(receipt.block_number)
            return
    with runtime.conn.cursor() as cur:
        cur.execute(MARK_REORGED, (STATUS_REORGED, row.id))
        audit(
            cur,
            Event.SETTLEMENT_REORGED,
            _payload(row, extra={"previous_status": row.status}),
            actor=Actor.WATCHER,
        )
    runtime.conn.commit()
    verdicts.reorged += 1
    verdicts.shaken_blocks.append(row.block_number)


def _finalize(runtime: WatcherRuntime, row: SettlementRow) -> None:
    """``pending -> final``, dengan ``status`` lama ikut di klausa WHERE.

    Perbandingan status di ``WHERE`` bukan hiasan: ia membuat UPDATE ini tidak
    bisa menghidupkan kembali baris yang sudah dipindahkan proses lain di
    antara SELECT dan UPDATE. Hanya satu Watcher yang jalan, tapi lock sesi
    adalah janji di tingkat aplikasi, sedangkan ini di tingkat baris.
    """
    with runtime.conn.cursor() as cur:
        cur.execute(MARK_FINAL, (STATUS_FINAL, row.id, STATUS_PENDING))
        if cur.rowcount != 1:
            runtime.conn.rollback()
            return
        audit(cur, Event.SETTLEMENT_FINALIZED, _payload(row), actor=Actor.WATCHER)
    runtime.conn.commit()


def _move(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    block_number: int,
    block_hash: str,
    status: str,
    event: Event,
) -> None:
    """Pindahkan baris ke blok lain. Kunci uniknya tidak berubah."""
    with runtime.conn.cursor() as cur:
        cur.execute(MOVE_BLOCK, (block_number, block_hash, status, row.id))
        audit(
            cur,
            event,
            _payload(row, extra={"new_block_number": block_number}),
            actor=Actor.WATCHER,
        )
    runtime.conn.commit()


def _touch(runtime: WatcherRuntime, row: SettlementRow) -> None:
    """Catat bahwa baris ini baru diperiksa, tanpa mengubah statusnya."""
    with runtime.conn.cursor() as cur:
        cur.execute(MARK_VERIFIED, (row.id,))
    runtime.conn.commit()


def _apply_rewind(runtime: WatcherRuntime, verdicts: VerifyVerdicts) -> int | None:
    """Mundurkan kursor ke sebelum blok paling awal yang goyah.

    Blok yang hash-nya berubah harus dipindai ulang: log di dalamnya bisa
    berbeda, dan log baru yang muncul di blok pengganti tidak akan pernah
    terlihat kalau kursor sudah melewatinya. Karena ``INSERT`` memakai
    ``ON CONFLICT DO NOTHING``, memindai ulang tidak pernah menghasilkan baris
    ganda — jadi memundurkan terlalu jauh hanya lambat, sedangkan tidak
    memundurkan sama sekali kehilangan data.

    Blok 0 adalah satu-satunya yang tidak bisa dimundurkan sepenuhnya: kolom
    ``last_scanned`` punya ``CHECK (>= 0)`` dan Watcher tidak punya DELETE,
    jadi kursornya dijepit di 0. Reorg blok genesis bukan hal yang terjadi.
    """
    if not verdicts.shaken_blocks:
        return None
    target = max(0, min(verdicts.shaken_blocks) - 1)
    with runtime.conn.cursor() as cur:
        current = read_cursor(cur, runtime.config.chain_id)
        if current == NO_BLOCK_SCANNED or current <= target:
            runtime.conn.rollback()
            return None
        write_cursor(cur, runtime.config.chain_id, target)
    runtime.conn.commit()
    return target


def _row_of(row: Sequence[object]) -> SettlementRow:
    return SettlementRow(
        id=to_int(row[0]),
        tx_hash=str(row[1]),
        log_index=to_int(row[2]),
        block_number=to_int(row[3]),
        block_hash=str(row[4]),
        status=str(row[5]),
    )


def _payload(row: SettlementRow, *, extra: dict[str, object] | None = None) -> dict[str, object]:
    """Payload audit untuk satu baris settlement.

    Tidak ada ``reason``, tidak ada URL, tidak ada bahan kunci — dan
    :func:`payment_guard.events.audit` menolaknya kalau ada. Yang dicatat
    adalah identitas baris di chain, yaitu yang bisa diperiksa ulang oleh orang
    lain di explorer.
    """
    payload: dict[str, object] = {
        "settlement_id": row.id,
        "tx_hash": row.tx_hash,
        "log_index": row.log_index,
        "block_number": row.block_number,
        "block_hash": row.block_hash,
    }
    if extra:
        payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# Perintah CLI
# --------------------------------------------------------------------------- #


def run_watch(
    config: WatcherConfig,
    *,
    client: ChainClient | None = None,
    loop: bool = False,
    cycles: int | None = None,
    from_block: int | None = None,
    out: TextIO | None = None,
    sleep: Callable[[int], object] = time.sleep,
) -> int:
    """Jalankan satu siklus, atau beberapa siklus dengan jeda dari config.

    ``cycles`` membatasi jumlah siklus; ``None`` dengan ``loop=True`` berarti
    sampai dihentikan. Jedanya dari ``WATCHER_INTERVAL_SECONDS``, bukan angka
    di dalam kode.

    Mengembalikan kode keluar: 0 bila tidak ada alarm, 1 bila ada baris
    ``final`` yang ter-reorg. Alarm memang harus terlihat di kode keluar, bukan
    hanya di antara baris keluaran.
    """
    stream = out if out is not None else None
    runtime = start_watcher(config, client=client, out=stream)
    alarms = 0
    try:
        remaining = cycles if cycles is not None else (None if loop else 1)
        first = True
        while remaining is None or remaining > 0:
            if not first:
                sleep(config.interval_seconds)
            report = scan_once(runtime, from_block=from_block if first else None, out=stream)
            alarms += report.alarms
            first = False
            if remaining is not None:
                remaining -= 1
            if remaining is not None and remaining <= 0:
                break
            if not loop and cycles is None:
                break
    finally:
        close_runtime(runtime)
    return 1 if alarms else 0


def settlement_counts(url: str, chain_id: int) -> dict[str, int]:
    """Jumlah baris ``settlements`` per status. Dipakai laporan dan tes."""
    counts = dict.fromkeys(SETTLEMENT_STATUSES, 0)
    with connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, count(*) FROM settlements WHERE chain_id = %s GROUP BY status",
            (chain_id,),
        )
        for status, count in cur.fetchall():
            counts[str(status)] = to_int(count)
    return counts


__all__ = [
    "NO_BLOCK_SCANNED",
    "OPEN_STATUSES",
    "SETTLEMENT_STATUSES",
    "STATUS_FINAL",
    "STATUS_PENDING",
    "STATUS_REORGED",
    "ScanReport",
    "SettlementRow",
    "WatcherError",
    "WatcherRuntime",
    "close_runtime",
    "open_runtime",
    "read_cursor",
    "run_watch",
    "scan_once",
    "settlement_counts",
    "start_watcher",
    "write_cursor",
]
