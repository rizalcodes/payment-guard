# Kebijakan Keamanan

## Lingkup proyek

Payment Guard adalah **proyek portofolio** yang berjalan di **testnet dan
devnet lokal saja**. Ia **tidak** ditujukan untuk dana nyata, dan tidak pernah
dijalankan terhadap jaringan utama mana pun.

Konsekuensinya langsung: jangan memakainya untuk menjaga uang sungguhan, dan
jangan menganggap bukti yang ada di repo ini sebagai bukti kesiapan produksi.
Batas-batas yang sudah diketahui ditulis apa adanya di
[README](README.md#status-verifikasi), [`docs/decisions.md`](docs/decisions.md),
dan bagian "Utang terbuka" di [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

## Cara melaporkan kerentanan

Pakai **GitHub Private Vulnerability Reporting**, lewat tab **Security** di
halaman repo ini. Jalur itu membuat laporannya tetap tertutup sampai ada
perbaikan, dan tidak menuntut pelapor menulis apa pun di tempat publik.

**Jangan** mengirim laporan ke alamat email pribadi, dan jangan menulis detail
kerentanan di issue publik, pull request, atau diskusi.

## Waktu respons

**Tidak ada jaminan waktu respons.** Ini proyek portofolio yang dikerjakan di
luar jadwal kerja, bukan produk dengan dukungan. Laporan akan dibaca, tapi
tidak ada SLA, tidak ada janji tanggal perbaikan, dan tidak ada program bounty.

## Catatan: kunci default Anvil

Kunci privat **akun #0 bawaan Anvil** bersifat **publik dan bukan rahasia**.
Anvil mencetaknya setiap kali start, dan nilainya ada di dokumentasi Foundry.
Siapa pun bisa memakainya di devnet mana pun; ia tidak pernah mengendalikan
dana di jaringan publik.

Literal kunci itu pernah ada di `tests/unit/test_chain.py` sebagai contoh input
**tidak sah** (kasus "tanpa 0x"). Ia sudah diganti dengan nilai sintetis di
commit `9302a59`, sejalan dengan keputusan sebelumnya di `6c71797` yang
mengeluarkan literal yang sama dari `tests/fake_chain.py`.

**Kunci itu masih ada di riwayat commit lama, dan itu disengaja.** Riwayat
tidak ditulis ulang: menulis ulang akan memindahkan puluhan hash dan
membatalkan tag yang sudah dipublikasikan, demi menyembunyikan nilai yang
memang publik. Jadi pemindai rahasia yang menyisir seluruh riwayat repo ini
bisa saja menyorotinya — itu temuan yang benar bentuknya, tapi bukan kebocoran.

Yang **tidak pernah** masuk repo, dan harus tetap begitu: berkas `.env` nyata,
kunci privat Guard, dan kunci wallet Executor. Berkas env yang ada di repo
hanya `.env*.example`.

## Catatan: konfigurasi gitleaks

Repo ini memakai [gitleaks](https://github.com/gitleaks/gitleaks) sebagai hook
pre-commit, dengan konfigurasi di [`.gitleaks.toml`](.gitleaks.toml).

Blok `[allowlist]` di sana **kosong dengan sengaja**: pemindaian seluruh
riwayat per 19 November 2026 tidak menemukan satu pun temuan, jadi tidak ada
yang perlu dikecualikan. Setiap entri yang ditambahkan kelak harus menyertakan
alasannya dan hash commit yang bersangkutan — allowlist tanpa alasan adalah
cara termudah mematikan pemindai tanpa ada yang menyadarinya.
