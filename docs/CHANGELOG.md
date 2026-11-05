# Changelog

Per tag, yang terbaru di atas. Isinya yang perlu diketahui orang yang memakai
atau meninjau tag itu — termasuk yang salah di dalamnya.

Alasan di balik keputusan ada di `docs/decisions.md`; berkas ini hanya mencatat
apa yang terjadi pada tag dan commit.

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
