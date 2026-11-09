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

from payment_guard.addresses import AddressError, normalize_address
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

#: Transisi status settlement yang **diizinkan**, sebagai ``(dari, ke)``.
#:
#: Diturunkan langsung dari tabel state machine di PRD v2 bagian 10, dan
#: ditulis sebagai **data** supaya daftarnya bisa dibaca dan diuji sekaligus.
#: Sampai M6 transisinya tersebar di empat fungsi, dan daftar yang dilarang
#: tidak ada di mana pun — jadi tidak ada yang menolak ``final -> pending``
#: selain kebetulan tidak ada pemanggil yang memintanya.
#:
#: Yang **tidak** ada di sini, dan alasannya:
#:
#: * ``final -> pending`` — membatalkan finality tanpa reorg. Rekonsiliasi
#:   hanya menghitung baris ``final``; menurunkannya kembali berarti pembayaran
#:   yang sudah dicocokkan berhenti dihitung tanpa satu pun alarm.
#: * ``reorged -> final`` — melompati ``pending``. Baris yang baru muncul lagi
#:   harus melewati kedalaman konfirmasi sekali lagi; rantai yang baru saja
#:   menata ulang dirinya adalah rantai yang paling mungkin menata ulang lagi.
#: * ``pending -> pending`` **tanpa** pindah blok, ``final -> final``, dan
#:   ``reorged -> reorged`` sebagai penulisan status: itu bukan transisi,
#:   melainkan pemeriksaan ulang. Jalurnya :func:`_touch`, yang hanya menyentuh
#:   ``last_verified_at``.
#:
#: ``(STATUS_PENDING, STATUS_PENDING)`` ada di daftar karena satu transisi nyata
#: memakainya: ``settlement_moved``, baris yang sama dipindah ke blok lain dan
#: tetap ``pending``.
SETTLEMENT_TRANSITIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        (STATUS_PENDING, STATUS_FINAL),
        (STATUS_PENDING, STATUS_PENDING),
        (STATUS_PENDING, STATUS_REORGED),
        (STATUS_REORGED, STATUS_PENDING),
        (STATUS_FINAL, STATUS_REORGED),
    }
)

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

#: Alamat pengirim yang pernah dipakai Executor, satu baris per alamat.
#:
#: **Hanya kolom ``from_addr``.** Itu satu-satunya kolom ``executions`` yang
#: boleh dibaca Watcher (``0003_watcher_from_addr.sql``), dan menyebut kolom
#: lain di sini — termasuk ``ORDER BY id`` — akan ditolak Postgres dengan
#: ``permission denied``. Query ini karena itu ditulis sesempit mungkin dengan
#: sengaja, dan tes integrasi memeriksa setiap kolom lain satu per satu.
#:
#: Satu kekecualian yang diuji dan patut diketahui: ``count(*)`` **berhasil**
#: dengan hak satu kolom, karena Postgres memperlakukannya sebagai tidak
#: merujuk kolom mana pun. Hak kolom menutup isi baris, bukan jumlahnya.
SELECT_EXECUTION_SENDERS: Final = """
SELECT DISTINCT from_addr FROM executions
"""

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

#: ``AND status = %s`` adalah status **lama**, sama seperti di ``MARK_FINAL``.
#:
#: Ditambahkan di M7. Tanpa itu, UPDATE ini menulis apa pun yang diminta ke
#: baris apa pun — termasuk baris yang statusnya sudah berubah di antara SELECT
#: dan UPDATE. Hanya satu Watcher yang jalan, tapi lock sesi adalah janji di
#: tingkat aplikasi; penjaga ini di tingkat baris, dan ia juga yang membuat
#: transisi terlarang tetap ditolak kalau suatu hari ada pemanggil baru.
MOVE_BLOCK: Final = """
UPDATE settlements
SET block_number = %s, block_hash = %s, status = %s, last_verified_at = now()
WHERE id = %s AND status = %s
"""

MARK_REORGED: Final = """
UPDATE settlements SET status = %s, last_verified_at = now()
WHERE id = %s AND status = %s
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
    #: Lebar rentang ``eth_getLogs`` terakhir yang **diterima** node, dalam blok.
    #:
    #: ``None`` berarti belum ada yang diterima di proses ini. Nilainya hidup
    #: di memori saja dan **tidak** disimpan ke database: ia pengetahuan tentang
    #: endpoint yang sedang dipakai, bukan tentang keadaan chain, dan endpoint
    #: bisa berganti di antara dua proses tanpa ada yang memberi tahu.
    #:
    #: Sebelum M6.7 lebar selalu dimulai dari sisa rentang batch, jadi batas
    #: node ditemukan ulang di **setiap** potongan. Pada endpoint publik yang
    #: menolak di atas ~200 blok, satu batch 2000 blok membuang 38 dari 52
    #: permintaan — dan penolakan memakan ~14,5 detik sementara permintaan yang
    #: diterima hanya ~0,75 detik. Mengingat satu angka ini menghapus seluruh
    #: pemborosan itu.
    accepted_range_width: int | None = None


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
        # Penjaga alamat wallet berjalan **sebelum** pemeriksaan chain, dan
        # sebelum pemindaian apa pun: ia tidak butuh jaringan, jadi tidak ada
        # alasan membelanjakan perjalanan RPC untuk konfigurasi yang sudah
        # salah.
        check_wallet_matches_executions(runtime)
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
# Penjaga alamat wallet (M6.2)
# --------------------------------------------------------------------------- #


def execution_senders(cur: psycopg.Cursor) -> list[str]:
    """Alamat pengirim berbeda yang pernah dipakai Executor, ternormalisasi.

    Hanya ``from_addr`` yang dibaca; lihat :data:`SELECT_EXECUTION_SENDERS`.

    Nilai di kolom itu sudah lowercase — ``CHECK (from_addr ~
    '^0x[0-9a-f]{40}$')`` di ``0001_init.sql`` menjaminnya — tapi tetap
    dinormalisasi lewat :func:`payment_guard.addresses.normalize_address`.
    Alasannya bukan ketidakpercayaan pada CHECK: ia memastikan kedua sisi
    perbandingan melewati fungsi yang **sama**, sehingga tidak ada bentuk yang
    bisa berbeda di satu sisi saja.

    Raises:
        WatcherError: bila ada nilai yang bukan alamat sah. Baris semacam itu
            tidak bisa ada selama CHECK berlaku, jadi kalau ia ada, yang salah
            bukan konfigurasi melainkan database.
    """
    cur.execute(SELECT_EXECUTION_SENDERS)
    senders = []
    for row in cur.fetchall():
        raw = str(row[0])
        try:
            senders.append(normalize_address(raw))
        except AddressError as exc:
            raise WatcherError(
                f"executions.from_addr memuat nilai yang bukan alamat ({raw!r}): {exc}. "
                "CHECK di 0001_init.sql seharusnya mencegah ini."
            ) from None
    return sorted(senders)


def check_wallet_matches_executions(runtime: WatcherRuntime) -> None:
    """Tolak start bila ``EXECUTOR_WALLET_ADDRESS`` bukan wallet yang dipakai.

    Keputusan CoS, opsi A. Konfigurasi yang menunjuk wallet lain adalah
    kegagalan yang **tidak** menggagalkan apa pun: Watcher jalan, menyaring log
    per pengirim yang salah, dan tidak pernah melihat satu pun pembayaran.
    Tidak ada pengecualian, tidak ada baris ``settlements``, tidak ada alarm —
    hanya diam. Satu kolom ``from_addr`` cukup untuk menolaknya lebih dulu.

    Tiga keadaan:

    * **Tidak ada eksekusi.** Lanjut. Belum ada apa pun untuk dibandingkan, dan
      menolak start di sini berarti Watcher tidak bisa dijalankan sebelum
      pembayaran pertama — padahal justru pembayaran pertama yang ingin
      dilihatnya.
    * **Semua ``from_addr`` sama dengan config.** Lanjut.
    * **Ada yang berbeda.** Tolak, catat audit, dan biarkan pemanggil keluar
      dengan kode non-nol.

    Audit ditulis di **transaksinya sendiri** dan di-commit sebelum
    pengecualian dilempar. Itu disengaja: kejadian ini satu-satunya jejak yang
    akan ada, karena sesudahnya tidak ada apa pun yang berjalan.

    Kedua alamat masuk ke pesan dan ke payload audit. Keduanya publik — yang
    rahasia adalah kuncinya, bukan alamatnya — dan operator yang salah mengisi
    `.env` tidak bisa memperbaikinya tanpa melihat keduanya.

    Raises:
        WatcherError: bila ada ``from_addr`` yang berbeda, atau bila alamat di
            konfigurasi tidak sah.
    """
    config = runtime.config
    try:
        expected = normalize_address(config.wallet_address)
    except AddressError as exc:
        raise WatcherError(
            f"EXECUTOR_WALLET_ADDRESS bukan alamat yang sah ({config.wallet_address!r}): {exc}"
        ) from None

    with runtime.conn.cursor() as cur:
        senders = execution_senders(cur)
    runtime.conn.rollback()

    if not senders:
        return
    unexpected = [address for address in senders if address != expected]
    if not unexpected:
        return

    with runtime.conn.cursor() as cur:
        audit(
            cur,
            Event.ALARM_WATCHER_WALLET_MISMATCH,
            {
                "expected_from_addr": expected,
                "found_from_addr": unexpected,
                "chain_id": config.chain_id,
            },
            actor=Actor.WATCHER,
        )
    runtime.conn.commit()

    raise WatcherError(
        "EXECUTOR_WALLET_ADDRESS tidak cocok dengan wallet yang dipakai Executor. "
        f"Diharapkan {expected}; di executions ditemukan {', '.join(unexpected)}. "
        "Watcher menyaring log Transfer per pengirim, jadi konfigurasi ini akan "
        "membuatnya tidak pernah melihat satu pun pembayaran. Perbaiki "
        "EXECUTOR_WALLET_ADDRESS di .env, atau jalankan Watcher untuk wallet yang "
        "benar. Alamat di atas publik; kuncinya tidak pernah dibaca Watcher."
    )


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

    Lebar **awal**-nya diambil dari :attr:`WatcherRuntime.accepted_range_width`
    — lebar terakhir yang diterima node di proses ini — dibatasi sisa rentang.
    Tanpa ingatan itu (potongan pertama sebuah proses) perilakunya sama dengan
    sebelum M6.7: mulai dari sisa rentang, lalu bagi dua sampai diterima.

    Ingatan itu yang menghapus pemborosan. Batas node ditemukan **sekali** per
    proses, bukan sekali per potongan: pada endpoint publik yang menolak di
    atas ~200 blok, satu batch 2000 blok turun dari 52 permintaan (38 terbuang,
    9,4 menit) menjadi 10 permintaan tanpa satu pun terbuang.

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
    remaining = last_block - first_block + 1
    remembered = runtime.accepted_range_width

    # Lebar awal: yang terakhir diterima node, dibatasi sisa rentang. Tanpa
    # ingatan, perilakunya sama dengan sebelum M6.7 — mulai dari sisa rentang.
    width = remaining if remembered is None else min(remaining, remembered)

    # Percobaan yang dibatasi sisa rentang tidak mengajarkan apa pun tentang
    # batas node: 50 blok diterima bukan kabar baru kalau 200 sudah diketahui
    # diterima. Tanpa penjaga ini, satu potongan terakhir yang kebetulan kecil
    # akan menurunkan ingatan untuk seluruh batch berikutnya.
    capped_by_remaining = remembered is not None and remaining < remembered

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
            # Lebar yang baru ditolak tidak boleh dicoba lagi di potongan
            # berikutnya. Ingatan diturunkan ke lebar yang akan dicoba
            # sekarang — sebuah dugaan, tapi dugaan yang langsung diuji di
            # iterasi ini. Ini juga yang membuat ingatan bisa turun ketika
            # batas node mengecil, bukan hanya naik.
            runtime.accepted_range_width = width
            continue
        if not capped_by_remaining:
            runtime.accepted_range_width = width
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
            if _set_status(runtime, row, to=STATUS_FINAL, event=Event.SETTLEMENT_FINALIZED):
                verdicts.finalized += 1
        else:
            # Di bawah kedalaman konfirmasi: tetap `pending`. Yang dicatat
            # hanya bahwa ia baru saja diperiksa, supaya operator bisa
            # membedakan "belum dalam" dari "tidak pernah dilihat lagi".
            _touch(runtime, row)
        return
    _handle_hash_change(runtime, row, verdicts=verdicts)


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
    if _set_status(
        runtime,
        row,
        to=STATUS_PENDING,
        event=Event.SETTLEMENT_RESTORED,
        block=(receipt.block_number, new_hash),
    ):
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
    # Satu-satunya perbedaan dari `pending -> reorged` adalah nama
    # kejadiannya, dan perbedaan itu memang yang penting: baris `final` yang
    # ter-reorg adalah janji ke rekonsiliasi yang dibatalkan, jadi ia alarm.
    if _set_status(runtime, row, to=STATUS_REORGED, event=Event.ALARM_FINAL_SETTLEMENT_REORGED):
        verdicts.alarms += 1
        verdicts.reorged += 1
        verdicts.shaken_blocks.append(row.block_number)


def _handle_hash_change(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    verdicts: VerifyVerdicts,
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
            if _set_status(
                runtime,
                row,
                to=STATUS_PENDING,
                event=Event.SETTLEMENT_MOVED,
                block=(receipt.block_number, new_hash),
            ):
                verdicts.moved += 1
                verdicts.shaken_blocks.append(row.block_number)
                verdicts.shaken_blocks.append(receipt.block_number)
            return
    if _set_status(runtime, row, to=STATUS_REORGED, event=Event.SETTLEMENT_REORGED):
        verdicts.reorged += 1
        verdicts.shaken_blocks.append(row.block_number)


def require_allowed_transition(from_status: str, to_status: str) -> None:
    """Tolak transisi status yang tidak ada di :data:`SETTLEMENT_TRANSITIONS`.

    Dipanggil sebelum setiap penulisan status, jadi daftar transisi di atas
    adalah satu-satunya tempat yang perlu dibaca untuk tahu apa yang mungkin.

    Raises:
        WatcherError: bila transisinya tidak diizinkan. Bukan ``ValueError``:
            pemanggilnya selalu Watcher, dan pesannya ditujukan ke operator.
    """
    if from_status not in SETTLEMENT_STATUSES:
        raise WatcherError(f"status asal tidak dikenal: {from_status!r}")
    if to_status not in SETTLEMENT_STATUSES:
        raise WatcherError(f"status tujuan tidak dikenal: {to_status!r}")
    if (from_status, to_status) not in SETTLEMENT_TRANSITIONS:
        raise WatcherError(
            f"transisi settlement {from_status} -> {to_status} tidak diizinkan. "
            "Daftar yang diizinkan ada di payment_guard.watcher."
            "SETTLEMENT_TRANSITIONS, diturunkan dari PRD v2 bagian 10."
        )


def _set_status(
    runtime: WatcherRuntime,
    row: SettlementRow,
    *,
    to: str,
    event: Event,
    block: tuple[int, str] | None = None,
) -> bool:
    """Satu-satunya jalur yang menulis ``settlements.status``.

    Mengembalikan ``True`` bila barisnya benar-benar berubah. ``False`` berarti
    status baris sudah bukan ``row.status`` lagi saat UPDATE dijalankan, jadi
    tidak ada yang ditulis dan tidak ada audit — keadaan yang seharusnya tidak
    mungkin karena hanya satu Watcher yang jalan, tapi yang penjaga tingkat
    baris di SQL tetap menolak.

    ``block`` mengisi ``(block_number, block_hash)`` baru untuk transisi yang
    memindahkan baris; ``None`` berarti hanya status yang berubah.

    Transisi diperiksa **sebelum** menyentuh database. Status dan audit berada
    di satu transaksi: baris yang statusnya berubah tanpa jejak di
    ``audit_log``, atau sebaliknya, lebih buruk daripada perubahan yang gagal
    seluruhnya.

    Raises:
        WatcherError: bila transisinya tidak diizinkan.
    """
    require_allowed_transition(row.status, to)

    extra: dict[str, object] = {"previous_status": row.status}
    with runtime.conn.cursor() as cur:
        if block is None:
            if to == STATUS_FINAL:
                cur.execute(MARK_FINAL, (to, row.id, row.status))
            else:
                cur.execute(MARK_REORGED, (to, row.id, row.status))
        else:
            block_number, block_hash = block
            extra["new_block_number"] = block_number
            cur.execute(MOVE_BLOCK, (block_number, block_hash, to, row.id, row.status))
        if cur.rowcount != 1:
            runtime.conn.rollback()
            return False
        audit(cur, event, _payload(row, extra=extra), actor=Actor.WATCHER)
    runtime.conn.commit()
    return True


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
    "SELECT_EXECUTION_SENDERS",
    "SETTLEMENT_STATUSES",
    "SETTLEMENT_TRANSITIONS",
    "STATUS_FINAL",
    "STATUS_PENDING",
    "STATUS_REORGED",
    "ScanReport",
    "SettlementRow",
    "WatcherError",
    "WatcherRuntime",
    "check_wallet_matches_executions",
    "close_runtime",
    "execution_senders",
    "open_runtime",
    "read_cursor",
    "require_allowed_transition",
    "run_watch",
    "scan_once",
    "settlement_counts",
    "start_watcher",
    "write_cursor",
]
