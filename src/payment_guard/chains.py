"""Chain yang didukung, dan pemetaannya ke ``chain_id``.

PRD v2 memakai dua representasi untuk chain yang sama, dan keduanya disengaja:

``chain``
    String di mandate dan di argumen tool ``check_payment`` (PRD v2 bagian 6.1
    dan 7). PRD v2 bagian 6.2: "``chain`` hanya ``base-sepolia``".

``chain_id``
    Integer di payload envelope (``"chain_id": 84532``), di kolom
    ``decisions.chain_id``, dan di EIP-155 saat Executor menandatangani
    transaksi.

Mandate menyimpan **string** saja. Pemetaan ke integer hidup di sini, satu
tempat, supaya ``84532`` tidak tersebar sebagai angka ajaib di beberapa modul.

Hanya ada satu entri, dan itu bukan kekurangan: PRD v2 bagian 2 menempatkan
multi-chain sebagai non-tujuan v1, dan seluruh sistem adalah testnet-only.
Menambah entri kedua kelak berarti meninjau ulang Watcher, Executor, dan
verifikasi ``decimals()`` sekaligus.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

from typing import Final

#: Nama chain -> ``chain_id`` EIP-155. Daftar tertutup.
CHAIN_IDS: Final[dict[str, int]] = {
    "base-sepolia": 84532,
}

#: Satu-satunya chain yang didukung v1.
BASE_SEPOLIA: Final = "base-sepolia"


class ChainError(ValueError):
    """Nama chain tidak dikenal. Error input, bukan keputusan."""


def is_supported_chain(name: object) -> bool:
    """True bila ``name`` adalah nama chain yang didukung."""
    return isinstance(name, str) and name in CHAIN_IDS


def chain_id_for(name: str) -> int:
    """``chain_id`` EIP-155 untuk ``name``.

    Raises:
        ChainError: bila chain-nya tidak didukung.
    """
    if not is_supported_chain(name):
        raise ChainError(
            f"chain {name!r} tidak didukung; yang didukung: {', '.join(sorted(CHAIN_IDS))}"
        )
    return CHAIN_IDS[name]


def supported_chains() -> tuple[str, ...]:
    """Nama chain yang didukung, terurut. Dipakai pesan error dan tes."""
    return tuple(sorted(CHAIN_IDS))


# --------------------------------------------------------------------------- #
# Devnet lokal
# --------------------------------------------------------------------------- #
#
# Devnet dipisahkan dari :data:`CHAIN_IDS` **dengan sengaja**, dan itu bukan
# kerapian belaka. ``CHAIN_IDS`` memvalidasi string ``chain`` di mandate dan di
# argumen tool ``check_payment``. Menambahkan ``devnet-local`` ke sana berarti
# setiap Guard produksi mulai menerima mandate devnet, dan satu salah ketik di
# mandate menjadi pembayaran di chain yang salah alih-alih error.
#
# Jadi devnet hidup di peta terpisah yang hanya aktif bila konfigurasi
# **eksplisit** memilihnya. Tanpa flag itu, perilakunya identik dengan sebelum
# devnet ada: satu chain, 84532.

#: Nama jaringan produksi. Ini yang berlaku bila tidak ada yang memilih.
NETWORK_PRODUCTION: Final = "base-sepolia"

#: Nama jaringan devnet lokal.
NETWORK_DEVNET: Final = "devnet"

#: Variabel yang memilih jaringan. Tidak disetel = produksi.
NETWORK_ENV_VAR: Final = "PAYMENT_GUARD_NETWORK"

#: Nama chain devnet lokal, dipakai di mandate devnet.
DEVNET_LOCAL: Final = "devnet-local"

#: Chain devnet LOKAL. Hanya aktif di jaringan :data:`NETWORK_DEVNET`.
#:
#: 31337 adalah chain id bawaan Anvil. Ia tidak pernah muncul di jaringan
#: publik mana pun, jadi ia tidak bisa tertukar dengan chain sungguhan.
DEVNET_CHAIN_IDS: Final[dict[str, int]] = {
    DEVNET_LOCAL: 31337,
}

#: Alamat sepuluh akun default Anvil, lowercase.
#:
#: **Kunci di belakang alamat-alamat ini PUBLIK.** Mnemonic-nya
#: ("test test test ... junk") diterbitkan di dokumentasi Foundry. Daftar ini
#: ada supaya Executor bisa **menolak** memakainya di luar devnet: kunci publik
#: di jaringan sungguhan berarti dana yang hilang dalam hitungan detik.
#:
#: Yang disimpan di sini alamatnya, bukan kuncinya — yang dibutuhkan hanya
#: kemampuan mengenali, bukan kemampuan menandatangani. Diambil dari
#: ``eth_accounts`` pada build Anvil yang dipin di ``docker-compose.yml``.
ANVIL_DEFAULT_ADDRESSES: Final[frozenset[str]] = frozenset(
    {
        "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
        "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
        "0x90f79bf6eb2c4f870365e785982e1f101e93b906",
        "0x15d34aaf54267db7d7c367839aaf71a00a2c6a65",
        "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc",
        "0x976ea74026e726554db657fa54763abd0c3a0aa9",
        "0x14dc79964da2c08b23698b3d3cc7ca32193d9955",
        "0x23618e81e3f5cdf7f54c3d65f7fbc0abf5b21e8f",
        "0xa0ee7a142d267c1f36714e4a8f75612f20a79720",
    }
)

#: Jaringan -> nama chain yang berlaku di jaringan itu.
_NETWORK_CHAINS: Final[dict[str, dict[str, int]]] = {
    NETWORK_PRODUCTION: CHAIN_IDS,
    NETWORK_DEVNET: DEVNET_CHAIN_IDS,
}


class NetworkError(ValueError):
    """Nama jaringan tidak dikenal. Error konfigurasi, bukan keputusan."""


def supported_networks() -> tuple[str, ...]:
    """Nama jaringan yang bisa dipilih, terurut."""
    return tuple(sorted(_NETWORK_CHAINS))


def normalize_network(value: object) -> str:
    """Nama jaringan dari konfigurasi, dengan produksi sebagai bawaan.

    ``None`` dan string kosong berarti produksi: tanpa flag, tidak ada yang
    berubah. Nama yang tidak dikenal **ditolak** alih-alih jatuh ke bawaan —
    ``PAYMENT_GUARD_NETWORK=devnett`` yang diam-diam berarti produksi adalah
    cara sebuah devnet berakhir menandatangani di chain sungguhan.

    Raises:
        NetworkError: bila namanya tidak dikenal.
    """
    if value is None:
        return NETWORK_PRODUCTION
    if not isinstance(value, str):
        raise NetworkError(f"{NETWORK_ENV_VAR} harus string, dapat {type(value).__name__}")
    nama = value.strip()
    if not nama:
        return NETWORK_PRODUCTION
    if nama not in _NETWORK_CHAINS:
        raise NetworkError(
            f"jaringan {nama!r} tidak dikenal; yang ada: {', '.join(supported_networks())}"
        )
    return nama


def is_devnet_network(network: str) -> bool:
    """True bila ``network`` adalah devnet lokal."""
    return normalize_network(network) == NETWORK_DEVNET


def chains_for_network(network: str) -> dict[str, int]:
    """Nama chain -> ``chain_id`` yang berlaku di ``network``.

    Raises:
        NetworkError: bila jaringannya tidak dikenal.
    """
    return dict(_NETWORK_CHAINS[normalize_network(network)])


def expected_chain_id_for_network(network: str) -> int:
    """Satu-satunya ``chain_id`` yang sah di ``network``.

    Setiap jaringan punya tepat satu chain, jadi "chain yang diharapkan" adalah
    pertanyaan yang punya satu jawaban. Inilah yang membuat penolakan dua arah
    jatuh sendiri: 31337 hanya sah di devnet, 84532 hanya sah di produksi.

    Raises:
        NetworkError: bila jaringannya tidak dikenal.
    """
    chains = chains_for_network(network)
    (chain_id,) = chains.values()
    return chain_id


def is_anvil_default_address(address: object) -> bool:
    """True bila ``address`` adalah salah satu akun default Anvil.

    Perbandingannya lowercase, karena ``normalize_address`` menyimpan lowercase.
    """
    return isinstance(address, str) and address.lower() in ANVIL_DEFAULT_ADDRESSES
