# Changelog

Per tag, yang terbaru di atas. Isinya yang perlu diketahui orang yang memakai
atau meninjau tag itu — termasuk yang salah di dalamnya.

Alasan di balik keputusan ada di `docs/decisions.md`; berkas ini hanya mencatat
apa yang terjadi pada tag dan commit.

---

## Utang terbuka

Hal yang **sengaja** belum dikerjakan, dengan alasannya. Dihapus dari daftar
ini hanya ketika benar-benar selesai.

*Kosong.* Utang satu-satunya — B3, cek alamat wallet saat start — ditutup di
M6.2; catatannya di bawah dibiarkan sebagai riwayat.

---

## M6.2 — penjaga alamat wallet lewat grant kolom-tunggal

Belum ditag.

`0003_watcher_from_addr.sql`: satu pernyataan,
`GRANT SELECT (from_addr) ON executions TO watcher_rw`. Tidak ada GRANT lain,
tidak ada perubahan tabel. Keputusan CoS, opsi A.

**Penjaganya.** Sebelum pemindaian apa pun — dan sebelum satu pun perjalanan
RPC — Watcher membaca `DISTINCT from_addr` dari `executions` dan
membandingkannya dengan `EXECUTOR_WALLET_ADDRESS` lewat `normalize_address`,
jadi alamat ber-checksum dari explorer tetap cocok. Tidak ada eksekusi: lanjut.
Semua sama: lanjut. Ada yang berbeda: menolak start, mencatat
`alarm_watcher_wallet_mismatch` di transaksinya sendiri, dan keluar dengan kode
non-nol.

Yang dicegah adalah kegagalan yang **tidak** menggagalkan apa pun: konfigurasi
yang menunjuk wallet lain membuat Watcher jalan, menyaring log per pengirim
yang salah, dan tidak pernah melihat satu pun pembayaran — tanpa pengecualian,
tanpa baris `settlements`, hanya diam.

**Matriks hak**, perubahan pertama sejak `0002_roles.sql`:

| | sebelum | sesudah |
|---|---|---|
| `executions` × `watcher` | `—` | `S (kolom from_addr saja)` |

`has_table_privilege('watcher_rw', 'executions', 'SELECT')` tetap `false`.
Ada tes terpisah untuk itu, karena setiap tes per kolom akan tetap lulus kalau
grant-nya diganti menjadi tingkat tabel.

**Dua kekeliruan saya sendiri yang ditemukan tes:**

1. `count(*)` **berhasil** dengan hak satu kolom. Saya menulis tesnya dengan
   `pytest.raises` lebih dulu dan ia gagal. Postgres memperlakukan `count(*)`
   sebagai tidak merujuk kolom mana pun, jadi hak pada satu kolom sudah cukup.
   Hak kolom menutup isi baris, bukan jumlahnya. Tesnya sekarang menyatakan
   yang benar dan menjelaskan kenapa dampaknya kecil.
2. Generator `docs/schema.md` menelan blok komentar ke dalam kolom "hak",
   karena `parse_grants` tidak memanggil `strip_comments` sementara polanya
   memakai `re.DOTALL`. `0003` adalah migrasi pertama dengan komentar panjang
   tepat sebelum pernyataannya, jadi bug itu baru terlihat sekarang.
   Diperbaiki di generatornya, bukan dengan memendekkan komentar.

**Tes.** `TestWalletAddressGuardAtStartup` dibuka dari `skip` dan tumbuh dari
3 menjadi 11 tes yang berjalan. `TestTheWatcherStillCannotReadExecutions`
digantikan `TestTheWatcherReadsOnlyFromAddr`: pernyataannya berubah dari
"tidak ada hak" menjadi "tepat satu kolom", dengan satu tes per kolom terlarang.
`test_select_is_never_granted_per_column` di `test_privileges.py` menjadi
`test_select_columns_match_matrix`, bentuknya kini sama dengan padanannya untuk
UPDATE.

Fixture `migrated` tidak lagi memeriksa daftar nama migrasi yang ditulis
tangan; ia membandingkan dengan `discover()`. Daftar tangan harus disunting
setiap kali migrasi bertambah, dan sampai disunting ia menggagalkan seluruh
suite integrasi dengan pesan yang tidak menyebut sebabnya — yang persis terjadi
saat `0003` ditambahkan.

**Operator harus menjalankan `guard db migrate` sendiri** sebelum
`guard watch`. Tidak ada proses runtime yang menerapkan migrasi, dan
`guard serve` hanya memeriksa keberadaan tabel (RR-12), bukan versinya.
Prosedurnya di `docs/runbook.md` bagian 9. Migrasi ini **belum** diterapkan di
cluster operator.

---

### B3 — cek alamat wallet saat start (DITUTUP di M6.2, opsi A)

Yang diminta: Watcher membandingkan `EXECUTOR_WALLET_ADDRESS` dengan
`from_addr` di `executions`, dan menolak start bila ada eksekusi dengan alamat
yang berbeda. Gunanya nyata — konfigurasi yang menunjuk wallet lain tidak
menggagalkan apa pun, ia hanya membuat Watcher tidak pernah melihat pembayaran.

Kenapa belum dikerjakan: itu butuh **SELECT pada `executions`**, dan
`0002_roles.sql` tidak memberikannya ke `watcher_rw`. Bukan kelalaian —
Arsitektur bagian 3.3 menaruh tanda hubung di sana dengan sengaja, supaya
proses yang menulis `settlements` tidak juga bisa membaca keputusan yang
mengizinkannya. Menambah grant itu melonggarkan pemisahan yang paling ingin
dijaga di proyek ini, dan itu bukan keputusan pelaksana.

Dua opsi untuk CoS:

| | Cara | Harga |
|---|---|---|
| **A** | Migrasi `0003` memberi `watcher_rw` SELECT pada `executions`; cek dilakukan Watcher seperti diminta. | Watcher jadi bisa membaca `executions`. Pemisahan yang dijaga M6 melemah, dan migrasi baru menyentuh matriks hak. |
| **B** | Cek dipindahkan ke Reconciler, yang **sudah** punya SELECT pada `executions` dan `settlements`. Ia melaporkan ketidakcocokan alamat sebagai temuan, bukan menolak start Watcher. | Tidak ada grant baru, tidak ada migrasi. Tapi ketidakcocokan baru terlihat saat `guard reconcile` dijalankan, bukan saat Watcher start. |

Opsi B tidak butuh perubahan hak apa pun dan karena itu yang akan dipilih
kalau tidak ada arahan lain — tapi ia **belum** dikerjakan, karena yang
diminta adalah penolakan saat start.

**Hasil: CoS memilih opsi A**, dengan hak dipersempit ke satu kolom.
Dikerjakan di M6.2; lihat entri di atas. Anjuran saya (opsi B) tidak diikuti,
dan alasannya masuk akal — Reconciler yang melaporkan belakangan tidak
mencegah Watcher memantau wallet yang salah selama berjam-jam lebih dulu.

Tes yang ditandai `skip` sudah dibuka dan dijalankan.
`TestTheWatcherStillCannotReadExecutions` sengaja gagal setelah grant berubah,
yang memang gunanya; ia digantikan `TestTheWatcherReadsOnlyFromAddr` yang
menjaga pernyataan yang lebih sempit: tepat satu kolom, bukan nol.

---

## M6.1 — aturan `--from-block`, `eth_getLogs` berpotongan, utang dokumen

Belum ditag.

**`--from-block` wajib sekali, dan tidak pernah dua kali.** Tanpa kursor di
`watcher_state`, Watcher berhenti dan menyebut flag itu; tidak ada default.
Dengan kursor, flag itu diabaikan dan peringatannya dicetak.

M6 memakai 0 sebagai default, dengan alasan bahwa memindai dari genesis lambat
tapi tidak pernah melewatkan apa pun. Itu salah untuk Base Sepolia: dari
genesis sampai kepala rantai ada puluhan juta blok, jadi kursornya praktis
tidak pernah sampai ke pembayaran hari ini. Watcher yang terlihat jalan dan
tidak akan pernah tiba lebih buruk daripada Watcher yang menolak start. Angka
yang benar hanya diketahui operator — blok di sekitar funding wallet Executor —
jadi ia yang menyebutkannya. Prosedurnya sekarang ada: `docs/runbook.md`
bagian 8, yang menutup rujukan menggantung yang dilaporkan di M6.

**`eth_getLogs` dipecah per rentang.** Lebar awal dari `SCAN_BATCH_SIZE`,
diturunkan dari 2000 ke 1000. Bila node menolak **karena lebarnya**, rentang
dibagi dua dan dicoba lagi, sampai lantai 10 blok. Pengenalannya lewat
pencocokan kata pada pesan error RPC (`range`, `too large`, `more than`,
`limit`), karena tidak ada kode error standar untuk ini. Kegagalan lain
dilempar apa adanya: node yang menolak karena filter salah akan menolak hal
yang sama dengan rentang separuh, hanya lebih lambat dan dengan pesan akhir
yang menyalahkan hal yang bukan penyebabnya.

Aturan batch tidak berubah: satu rentang = satu `eth_getLogs` + satu transaksi
yang menyimpan hasilnya dan memajukan kursor. Satu siklus sekarang bisa berisi
beberapa batch. Konsekuensinya disengaja: kalau rentang ketiga gagal, dua
rentang pertama tetap tersimpan dan kursornya tetap maju sampai akhir rentang
kedua — tidak ada blok yang terlewat, dan tidak ada blok yang ditandai sudah
dipindai padahal log-nya belum tersimpan.

**Dokumen.** Arsitektur naik ke v1.2 (nama berkas tetap `Architecture_v1.1.md`,
penomoran bagian tidak berubah): bagian 2.1 kini memuat tiga variabel env yang
ditambahkan M6. PRD v2.1 bagian 14 (peta jalan) direvisi: M6 = Watcher +
Reconciler inti, M7 = sisanya. PRD v2 tidak disunting — ia dokumen yang
ditutup, dan kepala PRD v2.1 sudah menyatakan bagian yang direvisi
menggantikan pendahulunya.

**TERTAHAN: B3,** cek alamat wallet saat start. Lihat bagian "Utang terbuka"
di bawah.

---

## M6 — Watcher dan Reconciler, **mock-tested, belum live**

Belum ditag. Tag `m6` hanya setelah tes live dan bukti on-chain ada.

Dua modul baru: `watcher.py` menulis `settlements` dan `watcher_state` dari log
`Transfer` USDC; `reconciler.py` mencocokkan `settlements` dengan `executions`
dan `decisions` dan menulis `reconciliations`. Dua perintah baru: `guard watch`
dan `guard reconcile`. Enam kejadian baru di `events.py`, semuanya nama yang
sudah ada di PRD v2 bagian 10 dan 11 — tidak ada yang dikarang.

**Yang sudah terbukti:** 49 tes integrasi terhadap Postgres 16 di cluster tes,
dengan chain tiruan. Transfer cocok, transfer dengan jumlah atau penerima
berbeda, transfer tanpa execution, blok yang diproses dua kali, kedalaman
konfirmasi di bawah dan tepat di batas, keempat transisi reorg termasuk alarm
`final -> reorged`, kursor yang mundur, execution tanpa settlement setelah
batas waktu, dan GRANT yang menolak Watcher membaca `executions` maupun
Reconciler menulis `executions`.

**Yang belum terbukti:** apa pun terhadap Base Sepolia sungguhan. Tidak ada
satu pun `eth_getLogs` yang pernah dikirim ke node nyata dari repo ini. Bentuk
log yang diasumsikan `decode_transfer_log` diuji terhadap log yang **kami**
susun, dan known-answer `topic0` ERC-20 adalah satu-satunya hal di jalur itu
yang diverifikasi ke sumber luar. Yang paling mungkin berbeda di chain nyata:
tipe yang dikembalikan web3 untuk `topics` dan `data`, dan perilaku node publik
pada rentang `eth_getLogs` yang lebar.

**Satu kekeliruan yang ditemukan tes, dan layak dicatat.** Rekonsiliasi semula
membandingkan `settlements.to_addr` dengan `executions.to_addr`. Keduanya
terdengar seperti hal yang sama dan bukan: pada transfer ERC-20, tujuan
**transaksinya** adalah kontrak token, dan penerima pembayaran ada di dalam
calldata. `executions.to_addr` karena itu selalu berisi alamat kontrak USDC.
Perbandingan itu melaporkan **setiap** pembayaran yang benar sebagai
`mismatch` — alarm yang selalu berbunyi, yang sama dengan tidak ada alarm.
Penerima sekarang dibandingkan dengan `decisions.recipient`.

Tiga variabel konfigurasi baru yang **tidak** ada di tabel Arsitektur v1.1
bagian 2.1: `EXECUTOR_WALLET_ADDRESS`, `WATCHER_INTERVAL_SECONDS`, dan
`RECONCILE_DEADLINE_SECONDS`. Alasan masing-masing ada di `docs/decisions.md`.
PRD dan Arsitektur tidak diubah.

---

## m5.1 — satu tes merah di dalam tag

`m5.1` punya satu tes merah (`test_only_example_env_files_are_tracked`) karena
`.env.test.example` belum ter-commit saat suite dijalankan. Diperbaiki di
`m5.2`. Tag `m5.1` tidak dipindahkan.

Penyebabnya bukan tesnya: tes itu membaca **indeks git**, jadi berkas `.env*`
baru lolos selama masih untracked dan baru gagal setelah masuk indeks. Suite
dijalankan sebelum `git add`, lalu commit-nya menambahkan berkas yang membuat
tes itu gagal. Pre-commit tidak menangkapnya karena pada saat itu tesnya memang
masih hijau.

Yang mencegahnya terulang adalah aturan rilis di `docs/runbook.md` bagian
"Rilis": suite penuh dijalankan pada checkout bersih, bukan di working tree
yang punya berkas untracked.

Tag `m5.1` dibiarkan menunjuk commit yang sama. Memindahkannya akan membuat tag
berhenti menunjuk apa yang sebenarnya diuji dan dilaporkan saat itu, dan
riwayat yang menyembunyikan kesalahan lebih buruk daripada riwayat yang
memuatnya.

---

## m5.2 — atribusi diperbaiki, isi tidak berubah

Commit M6.0 ditulis ulang pesannya saja, untuk mengganti baris
`Co-Authored-By: Claude Haiku 5.5` menjadi `Claude Opus 5` — model yang
sesungguhnya mengerjakannya, menurut transkrip sesinya.

Pemetaan hash:

| commit | lama | baru | tree |
|---|---|---|---|
| M6.0 | `6c8adf6` | `2c72a1d` | `078f688` (tidak berubah) |
| M5.1 | `b675851` | `b675851` (tidak disentuh) | `252b77f` (tidak berubah) |

Tag yang diarahkan ulang:

| tag | lama | baru |
|---|---|---|
| `m5.2` | `6c8adf6` | `2c72a1d` |

`m5.1` dan `m0`–`m5` tidak terdampak: commit-nya tidak berubah. Atribusi di
`b675851` sudah benar sejak semula, jadi commit itu tidak ditulis ulang.

Tree hash kedua commit dibandingkan sebelum dan sesudah, dan identik:
`git diff 6c8adf6 2c72a1d` kosong. Cadangan ada di cabang
`backup-pre-reword`.
