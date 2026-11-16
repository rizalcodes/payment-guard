"""Pemilih jaringan: produksi secara bawaan, devnet lokal hanya bila diminta.

Devnet lokal (chain id 31337) dipisahkan dari chain produksi dengan sengaja.
Berkas ini menjaga empat hal, dan ketiga yang pertama lebih penting daripada
yang terakhir:

1. **Tanpa flag, tidak ada yang berubah.** ``CHAIN_IDS`` tetap satu entri, dan
   ``devnet-local`` tidak pernah bisa dipakai sebagai nama chain di mandate
   produksi.
2. **Penolakan dua arah.** 31337 hanya sah di devnet; 84532 hanya sah di
   produksi. Tidak ada jaringan yang menerima keduanya.
3. **Kunci default Anvil ditolak di luar devnet.** Kunci itu publik.
4. Nama jaringan yang tidak dikenal ditolak, bukan jatuh ke bawaan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from payment_guard.chain import ChainError, load_wallet
from payment_guard.chains import (
    ANVIL_DEFAULT_ADDRESSES,
    BASE_SEPOLIA,
    CHAIN_IDS,
    DEVNET_CHAIN_IDS,
    DEVNET_LOCAL,
    NETWORK_DEVNET,
    NETWORK_ENV_VAR,
    NETWORK_PRODUCTION,
    NetworkError,
    chains_for_network,
    expected_chain_id_for_network,
    is_anvil_default_address,
    is_devnet_network,
    normalize_network,
    supported_networks,
)
from payment_guard.config import GuardConfigError, load_executor_config, load_watcher_config

#: Mnemonic bawaan Anvil, diterbitkan di dokumentasi Foundry.
#:
#: Kuncinya **diturunkan** di sini alih-alih ditulis sebagai literal heksa, dan
#: itu disengaja: literal kunci privat memicu gitleaks (``generic-api-key``,
#: entropi 3,85) dan menggagalkan pre-commit. Membungkam pemindai rahasia demi
#: satu fixture adalah harga yang terlalu mahal — jadi yang disimpan di repo
#: adalah dua belas kata beretropi rendah ini, dan kuncinya dihitung saat tes
#: berjalan.
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"

#: Akun pertama Anvil. Kuncinya PUBLIK; ia ada di sini justru untuk diuji
#: penolakannya.
ANVIL_ADDR_0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"


def anvil_key_0() -> str:
    """Kunci privat akun pertama Anvil, diturunkan dari mnemonic-nya."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    akun = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/0")
    return str(akun.key.hex() if akun.key.hex().startswith("0x") else "0x" + akun.key.hex())


#: Kunci uji yang BUKAN akun default Anvil. Sama dengan `tests/fake_chain.py`.
LAIN_KEY = "0x4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b"
LAIN_ADDR = "0x504e4fbab7bc2962b361e3730b965e61f289d0cc"


def tulis_kunci(directory: Path, *, address: str, private_key: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "wallet.json"
    path.write_text(json.dumps({"address": address, "private_key": private_key}), encoding="utf-8")
    return path


class TestProductionIsUntouched:
    """Daftar chain produksi tidak boleh ikut melebar karena devnet ada."""

    def test_chain_ids_still_has_exactly_one_entry(self) -> None:
        assert CHAIN_IDS == {"base-sepolia": 84532}

    def test_devnet_is_not_in_the_production_map(self) -> None:
        """Kalau ia masuk sini, mandate produksi mulai menerima chain devnet."""
        assert DEVNET_LOCAL not in CHAIN_IDS

    def test_the_devnet_map_is_separate_and_has_one_entry(self) -> None:
        assert DEVNET_CHAIN_IDS == {"devnet-local": 31337}

    def test_the_default_network_is_production(self) -> None:
        assert normalize_network(None) == NETWORK_PRODUCTION
        assert normalize_network("") == NETWORK_PRODUCTION
        assert normalize_network("   ") == NETWORK_PRODUCTION

    def test_production_expects_base_sepolia(self) -> None:
        assert expected_chain_id_for_network(NETWORK_PRODUCTION) == 84532
        assert chains_for_network(NETWORK_PRODUCTION) == {BASE_SEPOLIA: 84532}

    def test_devnet_expects_31337(self) -> None:
        assert expected_chain_id_for_network(NETWORK_DEVNET) == 31337

    def test_only_two_networks_exist(self) -> None:
        assert supported_networks() == ("base-sepolia", "devnet")


class TestUnknownNetworkIsRefused:
    """Nama yang salah ketik tidak boleh diam-diam berarti produksi."""

    @pytest.mark.parametrize("nama", ["devnett", "DEVNET", "local", "mainnet", "base_sepolia"])
    def test_a_name_that_is_not_known_is_refused(self, nama: str) -> None:
        with pytest.raises(NetworkError) as info:
            normalize_network(nama)
        assert nama in str(info.value)

    def test_a_non_string_is_refused(self) -> None:
        with pytest.raises(NetworkError):
            normalize_network(31337)

    def test_is_devnet_network_refuses_an_unknown_name(self) -> None:
        with pytest.raises(NetworkError):
            is_devnet_network("devnett")


class TestExecutorRefusesBothDirections:
    """31337 hanya di devnet, 84532 hanya di produksi."""

    @staticmethod
    def _env(tmp_path: Path, *, chain_id: str, network: str | None) -> Path:
        kunci = tulis_kunci(tmp_path / "luar-repo", address=LAIN_ADDR, private_key=LAIN_KEY)
        keys = tmp_path / "guard_public.json"
        keys.write_text(json.dumps({"keys": []}), encoding="utf-8")
        baris = [
            "EXECUTOR_DB_URL=postgresql://x@127.0.0.1:5436/payment_guard_devnet",
            f"EXECUTOR_KEY_PATH={kunci}",
            f"GUARD_PUBLIC_KEYS_PATH={keys}",
            "RPC_URL=http://127.0.0.1:8545",
            f"CHAIN_ID={chain_id}",
        ]
        if network is not None:
            baris.append(f"{NETWORK_ENV_VAR}={network}")
        env = tmp_path / ".env"
        env.write_text("\n".join(baris) + "\n", encoding="utf-8")
        return env

    def test_production_network_refuses_the_devnet_chain_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="31337", network=None)
        with pytest.raises(GuardConfigError) as info:
            load_executor_config(env, repo_root=tmp_path / "repo")
        pesan = str(info.value)
        assert "31337" in pesan
        assert "84532" in pesan

    def test_devnet_network_refuses_base_sepolia(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="84532", network=NETWORK_DEVNET)
        with pytest.raises(GuardConfigError) as info:
            load_executor_config(env, repo_root=tmp_path / "repo")
        pesan = str(info.value)
        assert "84532" in pesan
        assert "31337" in pesan

    def test_devnet_network_accepts_31337(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="31337", network=NETWORK_DEVNET)
        config = load_executor_config(env, repo_root=tmp_path / "repo")
        assert config.chain_id == 31337
        assert config.network == NETWORK_DEVNET

    def test_no_flag_defaults_to_base_sepolia(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tanpa flag dan tanpa CHAIN_ID, nilainya 84532 seperti sebelumnya."""
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="", network=None)
        config = load_executor_config(env, repo_root=tmp_path / "repo")
        assert config.chain_id == 84532
        assert config.network == NETWORK_PRODUCTION

    def test_an_unknown_network_name_stops_the_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="31337", network="devnett")
        with pytest.raises(GuardConfigError) as info:
            load_executor_config(env, repo_root=tmp_path / "repo")
        assert NETWORK_ENV_VAR in str(info.value)


class TestWatcherRefusesBothDirections:
    """Watcher memakai pemilih jaringan yang sama."""

    @staticmethod
    def _env(tmp_path: Path, *, chain_id: str, network: str | None) -> Path:
        baris = [
            "WATCHER_DB_URL=postgresql://x@127.0.0.1:5436/payment_guard_devnet",
            "RPC_URL=http://127.0.0.1:8545",
            f"CHAIN_ID={chain_id}",
            f"EXECUTOR_WALLET_ADDRESS={LAIN_ADDR}",
        ]
        if network is not None:
            baris.append(f"{NETWORK_ENV_VAR}={network}")
        env = tmp_path / ".env"
        env.write_text("\n".join(baris) + "\n", encoding="utf-8")
        return env

    def test_production_network_refuses_the_devnet_chain_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="31337", network=None)
        with pytest.raises(GuardConfigError) as info:
            load_watcher_config(env)
        assert "31337" in str(info.value)

    def test_devnet_network_refuses_base_sepolia(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="84532", network=NETWORK_DEVNET)
        with pytest.raises(GuardConfigError) as info:
            load_watcher_config(env)
        assert "84532" in str(info.value)

    def test_devnet_network_accepts_31337(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
        env = self._env(tmp_path, chain_id="31337", network=NETWORK_DEVNET)
        config = load_watcher_config(env)
        assert config.chain_id == 31337


class TestAnvilKeysAreRefusedOutsideDevnet:
    """Kunci default Anvil publik, jadi ia hanya boleh dipakai di devnet."""

    def test_all_ten_default_addresses_are_recognised(self) -> None:
        assert len(ANVIL_DEFAULT_ADDRESSES) == 10
        for alamat in ANVIL_DEFAULT_ADDRESSES:
            assert is_anvil_default_address(alamat)

    def test_recognition_ignores_case(self) -> None:
        assert is_anvil_default_address(ANVIL_ADDR_0.upper())

    def test_a_non_anvil_address_is_not_flagged(self) -> None:
        assert not is_anvil_default_address(LAIN_ADDR)

    def test_the_anvil_key_is_refused_on_production(self, tmp_path: Path) -> None:
        path = tulis_kunci(tmp_path, address=ANVIL_ADDR_0, private_key=anvil_key_0())
        with pytest.raises(ChainError) as info:
            load_wallet(path)
        pesan = str(info.value)
        assert "PUBLIK" in pesan
        assert ANVIL_ADDR_0 in pesan
        assert NETWORK_ENV_VAR in pesan

    def test_the_refusal_never_quotes_the_private_key(self, tmp_path: Path) -> None:
        """Pesan error menyebut alamatnya, tidak pernah bahan kuncinya."""
        path = tulis_kunci(tmp_path, address=ANVIL_ADDR_0, private_key=anvil_key_0())
        with pytest.raises(ChainError) as info:
            load_wallet(path)
        pesan = str(info.value)
        kunci = anvil_key_0()
        assert kunci not in pesan
        assert kunci[2:] not in pesan

    def test_the_anvil_key_is_accepted_on_devnet(self, tmp_path: Path) -> None:
        path = tulis_kunci(tmp_path, address=ANVIL_ADDR_0, private_key=anvil_key_0())
        wallet = load_wallet(path, network=NETWORK_DEVNET)
        assert wallet.address == ANVIL_ADDR_0

    def test_a_non_anvil_key_is_accepted_on_production(self, tmp_path: Path) -> None:
        path = tulis_kunci(tmp_path, address=LAIN_ADDR, private_key=LAIN_KEY)
        wallet = load_wallet(path)
        assert wallet.address == LAIN_ADDR

    def test_production_is_the_default_for_load_wallet(self, tmp_path: Path) -> None:
        """Lupa meneruskan ``network`` berarti aman, bukan permisif."""
        path = tulis_kunci(tmp_path, address=ANVIL_ADDR_0, private_key=anvil_key_0())
        with pytest.raises(ChainError):
            load_wallet(path)


class TestTheRpcChainIdMustMatchTheConfiguration:
    """Konfigurasi yang benar tidak menolong kalau node-nya chain lain.

    ``check_startup`` sudah menolak selisih ini sebelum devnet ada; kelas ini
    mengikatnya ke dua arah devnet, supaya pengaman itu tidak bisa hilang
    tanpa satu tes memerah.
    """

    @staticmethod
    def _client(chain_id_value: int) -> object:
        from fake_chain import FakeChain

        return FakeChain(chain_id_value=chain_id_value)  # type: ignore[arg-type]

    def test_devnet_config_refuses_a_base_sepolia_node(self) -> None:
        from payment_guard.chain import check_startup

        with pytest.raises(ChainError, match="chain id dari RPC"):
            check_startup(
                self._client(84532),  # type: ignore[arg-type]
                expected_chain_id=expected_chain_id_for_network(NETWORK_DEVNET),
                usdc_address=LAIN_ADDR,
                wallet_address=LAIN_ADDR,
                minimum_balance_wei=0,
            )

    def test_production_config_refuses_a_devnet_node(self) -> None:
        from payment_guard.chain import check_startup

        with pytest.raises(ChainError, match="chain id dari RPC"):
            check_startup(
                self._client(31337),  # type: ignore[arg-type]
                expected_chain_id=expected_chain_id_for_network(NETWORK_PRODUCTION),
                usdc_address=LAIN_ADDR,
                wallet_address=LAIN_ADDR,
                minimum_balance_wei=0,
            )
