# Changelog

Per tag, yang terbaru di atas. Isinya yang perlu diketahui orang yang memakai
atau meninjau tag itu — termasuk yang salah di dalamnya.

Alasan di balik keputusan ada di `docs/decisions.md`; berkas ini hanya mencatat
apa yang terjadi pada tag dan commit.

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
