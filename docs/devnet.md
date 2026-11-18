# Devnet lokal

> **Ini devnet lokal, bukan testnet.** Chain id 31337, hidup dan mati di
> `127.0.0.1`. Tidak ada hubungannya dengan Base Sepolia (84532), dan token di
> dalamnya **bukan USDC**.
>
> Devnet ini ada sebagai pengganti bukti on-chain sebelum rilis (keputusan CoS
> 16 Nov 2026, dicatat di [decisions.md](decisions.md)). Ia **tidak**
> menggantikan bukti testnet publik, dan tidak boleh dilaporkan sebagai bukti
> testnet.

Hasil run skenario devnet dan demo jalur MCP tidak dicatat di sini, melainkan di
[demo-mcp-devnet.md](demo-mcp-devnet.md) dan [decisions.md](decisions.md);
dokumen ini hanya prosedur, versi, dan peringatan.

## Peringatan keamanan, dibaca lebih dulu

**Kunci default Anvil adalah PUBLIK.** Mnemonic bawaannya
(`test test test test test test test test test test test junk`) diterbitkan di
dokumentasi Foundry dan dipakai setiap pengembang di dunia. Alamat pertamanya,
`0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266`, adalah salah satu alamat paling
terkenal di Ethereum.

Konsekuensinya satu dan mutlak:

- **Jangan pernah memakai kunci ini di jaringan sungguhan**, termasuk testnet
  publik seperti Base Sepolia. Alamat itu dipantau bot; dana apa pun di sana
  hilang dalam hitungan detik.
- Devnet aman **hanya** karena chain-nya terikat ke `127.0.0.1` dan state-nya
  sekali pakai.
- `.env.devnet` ter-ignore git lewat pola `.env.*`; hanya
  `.env.devnet.example` yang di-track.

Kunci wallet Executor untuk devnet dibuat terpisah oleh skrip devnet dan juga
hanya berlaku di devnet.

## Versi yang dipin

Foundry **tidak lagi merilis tag semver stabil** — yang diterbitkan hanya
nightly bernama commit. Jadi pin-nya adalah commit itu sendiri, bukan `latest`
dan bukan `nightly`.

| | Nilai |
|---|---|
| Image | `ghcr.io/foundry-rs/foundry:nightly-cd3767105be7fa27eae63743c26359cefa0214fb` |
| Digest | `sha256:d41b25e26c3b63f415a0e425115f57ac372299902cdeeaf521ecd6cf595bab0b` |
| `anvil --version` | `1.9.1-nightly` |
| `forge --version` | `1.9.1-nightly` |
| Commit SHA | `cd3767105be7fa27eae63743c26359cefa0214fb` |
| Build timestamp | `2026-11-15T06:47:39Z` |
| Diverifikasi | 16 Nov 2026 |

Angka yang sama ada di komentar `docker-compose.yml` pada layanan `anvil`.

## Port dan cluster

| Apa | Port | Catatan |
|---|---|---|
| Anvil (devnet) | `127.0.0.1:8545` | profile `devnet` |
| Postgres devnet | `127.0.0.1:5436` | cluster **ketiga**, database `payment_guard_devnet` |
| Postgres operator | `127.0.0.1:5434` | **jangan disentuh devnet** |
| Postgres tes | `127.0.0.1:5435` | milik tes integrasi |

Cluster devnet terpisah karena alasan yang sama seperti cluster tes: role
Postgres bersifat **cluster-wide**, dan skrip devnet membuat serta menghapus
role. Devnet tidak boleh bisa merusak `.env` operator.

## Menjalankan

```bash
docker compose --profile devnet up -d anvil postgres_devnet
```

Profile `devnet` berarti `docker compose up -d` biasa **tidak** menyentuhnya.

## Satu jebakan di image Foundry, yang sudah menggigit sekali

ENTRYPOINT image itu adalah `["/bin/sh","-c"]`, bukan `anvil`. Akibatnya:

```yaml
# SALAH — flag hilang tanpa pesan error
command: ["anvil", "--host", "0.0.0.0", "--chain-id", "31337"]
```

Hanya `anvil` yang menjadi skrip `-c`; sisanya menjadi `$0 $1 …` dan diabaikan.
Anvil lalu mendengar di `127.0.0.1` **di dalam** container. docker-proxy tetap
menerima koneksi TCP di host, jadi port-nya tampak hidup — tapi setiap
permintaan RPC mati dengan `Remote end closed connection without response`.

Yang benar adalah meng-override entrypoint-nya:

```yaml
entrypoint: ["anvil"]
command: ["--host", "0.0.0.0", "--chain-id", "31337"]
```

Gejalanya menyesatkan: port terbuka, container "Up", log anvil normal kecuali
satu kata di baris `Listening on`.

## RPC Anvil yang dipakai skenario — diverifikasi di build yang dipin

Diuji langsung terhadap `1.9.1-nightly` di atas, bukan hanya dibaca dari
dokumentasi.

| Metode | Hasil uji | Catatan |
|---|---|---|
| `eth_chainId` | `0x7a69` | = 31337 |
| `anvil_mine` | bekerja | `[blocks]`, opsional `[blocks, interval]` |
| `anvil_setBalance` | bekerja | `[address, wei]` |
| `anvil_reorg` | **bekerja** | parameter **posisional**: `[depth, tx_block_pairs]` |
| `anvil_snapshot` / `evm_snapshot` | bekerja | `evm_*` adalah alias |
| `anvil_revert` / `evm_revert` | tersedia | alias, per dokumentasi |

**`anvil_reorg` menolak bentuk map.** `[{"depth": 2, "txBlockPairs": []}]`
dijawab `-32602 invalid type: map, expected u64`, dan `[2]` saja dijawab
`-32602 invalid length 1, expected struct ReorgOptions with 2 elements`. Yang
diterima hanya `[2, []]`. Perilakunya sesuai dokumentasi: rantai digulung
sedalam `depth` lalu ditambang kembali ke tinggi yang sama — tinggi blok
sebelum dan sesudah sama, isinya berbeda.

Karena `anvil_reorg` ada di build ini, skenario reorg memakainya, bukan
`evm_snapshot`/`evm_revert`. Snapshot/revert membatalkan state secara total;
`anvil_reorg` menghasilkan hal yang sebenarnya ingin diuji — transaksi yang
hilang dari rantai kanonik lalu bisa muncul lagi.

## Kontrak token uji — BUKAN USDC

Sumbernya di [../contracts/src/TestUSDC.sol](../contracts/src/TestUSDC.sol).

> **Kontrak ini token UJI.** Ia **bukan** USDC, tidak diaudit, dan tidak
> berhubungan dengan Circle maupun kontrak USDC mana pun. Ia hanya untuk devnet
> lokal (chain id 31337) dan tidak boleh di-deploy ke jaringan lain.
>
> **Alamatnya berubah setiap devnet dibangun ulang.** Setiap `devnet_up`
> men-deploy ulang dari nol, jadi `USDC_ADDRESS` di `.env.devnet` adalah nilai
> sekali pakai. Alamat yang di-hardcode di mana pun akan salah pada build
> berikutnya.

| | |
|---|---|
| Nama | `Test USDC (devnet lokal)` |
| Simbol | **`tUSDC`** — sengaja bukan `USDC`, supaya tidak mengundang salah baca di log maupun penjelajah blok |
| `decimals()` | **6** — satu-satunya kesamaan yang disengaja dengan USDC, dan ia wajib: Guard menolak start bila bukan 6, dan seluruh aritmetika uangnya memakai base unit `10**6` |
| Fungsi | `mint` (hanya owner), `transfer`, `approve`, `transferFrom`, `balanceOf`, `allowance`, `totalSupply` |
| Yang **tidak** ada | pause, blocklist, permit, upgrade, burn, batas pasokan |

Permukaannya sengaja kecil. Setiap fitur tambahan adalah hal yang harus dibaca
ulang oleh orang yang meninjau devnet ini, tanpa menambah satu pun jaminan
yang Payment Guard butuhkan.

### Kompilasi dan tes, tanpa dependensi eksternal

solc 0.8.28 sudah ada di dalam image Foundry yang dipin, dan versinya dipin
lagi di `contracts/foundry.toml` — tanpa itu, bytecode bisa berbeda antar
mesin, dan "berhasil di mesin saya" menjadi kalimat yang tidak bisa diperiksa.

Tesnya **tidak** memakai `forge-std`. Pustaka itu harus diunduh
(`forge install`), dan devnet ini dibangun di mesin yang jaringannya terbatas.
Tiga cheatcode yang dibutuhkan (`prank`, `expectRevert`, `expectEmit`)
dideklarasikan sebagai antarmuka kecil di berkas tesnya sendiri, sehingga
seluruh kompilasi dan tes berjalan **offline**.

```bash
docker run --rm -v "D:\payment guard\contracts:/work" -w /work \
  --entrypoint forge \
  ghcr.io/foundry-rs/foundry:nightly-cd3767105be7fa27eae63743c26359cefa0214fb \
  test -vv
```

Hasil pada 16 Nov 2026: **15 lulus, 0 gagal, 0 skipped**.

Artefak build (`contracts/out/`, `contracts/cache/`) di-gitignore: ia dibangun
ulang dari sumber dengan solc yang dipin.

## Catatan untuk yang menjalankan dari shell terbatas

Pada mesin tempat devnet ini dibangun, `curl` tidak bisa menjangkau
`127.0.0.1:8545` meski socket Python bisa. Kalau RPC tampak mati, uji dulu
dengan soket biasa sebelum menyimpulkan devnet-nya rusak:

```python
import json, urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8545",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode(),
    headers={"Content-Type": "application/json"},
)
print(json.load(urllib.request.urlopen(req, timeout=10)))
```
