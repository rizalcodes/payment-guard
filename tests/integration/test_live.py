"""Tes yang menyentuh Base Sepolia sungguhan. **Dilewati secara bawaan.**

Cara menjalankannya
-------------------

Tiga hal harus benar sekaligus, dan tidak ada satu pun yang bawaan:

1. ``-m live`` di baris perintah. ``addopts`` di ``pyproject.toml`` menyetel
   ``-m "not live"``, jadi tanpa ini tes di berkas ini tidak dikumpulkan.
2. ``PAYMENT_GUARD_LIVE=1`` di environment.
3. ``.env`` yang sah: ``RPC_URL`` dan ``EXECUTOR_KEY_PATH`` yang benar-benar
   ada, dengan ``EXECUTOR_KEY_PATH`` di luar akar repo.

```
set PAYMENT_GUARD_LIVE=1
uv run --no-sync pytest tests/integration/test_live.py -m live -q
```

Dua kunci, bukan satu, dengan sengaja: ``-m live`` saja bisa diketik tanpa
sadar oleh siapa pun yang menjalankan `-m ""`, dan variabel environment saja
bisa tertinggal di shell. Keduanya bersama tidak terjadi karena kecelakaan.

Apa yang **tidak** ada di sini
------------------------------

Tidak ada tes yang menyiarkan transaksi. Pembayaran sungguhan dikerjakan
operator secara manual lewat ``scripts/demo_execute.md``, satu perintah pada
satu waktu. Tes otomatis yang membelanjakan uang adalah tes yang suatu hari
akan berjalan ketika tidak ada yang mengharapkannya — di CI, di hook, di
perintah yang salah ketik.

Jadi yang diuji di sini hanya **pembacaan**: apakah endpoint yang dikonfigurasi
operator benar-benar Base Sepolia, apakah alamat USDC-nya benar-benar kontrak
dengan enam desimal, dan apakah wallet-nya punya gas. Ketiganya adalah
pemeriksaan start Executor, dijalankan terhadap jaringan sungguhan.

Fixture respons RPC yang direkam akan disimpan di milestone berikutnya. Yang
disimpan nanti tidak boleh memuat URL RPC maupun bahan kunci.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from payment_guard.chain import REQUIRED_DECIMALS, Web3Client
from payment_guard.config import ExecutorConfig, GuardConfigError, load_executor_config
from payment_guard.db import endpoint_host
from payment_guard.executor import MINIMUM_GAS_BALANCE_WEI, check_startup

pytestmark = [pytest.mark.live, pytest.mark.integration]

#: Variabel yang harus bernilai tepat ``"1"``.
LIVE_ENV_VAR = "PAYMENT_GUARD_LIVE"
LIVE_ENV_VALUE = "1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def live_config() -> ExecutorConfig:
    """Konfigurasi Executor dari ``.env`` operator, hanya untuk tes live.

    Ini satu-satunya tempat di seluruh suite yang membaca ``.env`` proyek, dan
    ia hanya bisa sampai di sini bila operator menyatakan niatnya dua kali.
    Harness integrasi lain tidak pernah membacanya; lihat kepala
    ``conftest.py``.
    """
    if os.environ.get(LIVE_ENV_VAR) != LIVE_ENV_VALUE:
        pytest.skip(
            f"{LIVE_ENV_VAR} bukan {LIVE_ENV_VALUE!r}. Tes live menyentuh chain "
            "sungguhan; ia tidak berjalan tanpa dinyatakan."
        )
    try:
        return load_executor_config(_repo_root() / ".env", repo_root=_repo_root())
    except GuardConfigError as exc:
        pytest.skip(f"konfigurasi Executor belum siap untuk tes live: {exc}")


@pytest.fixture(scope="module")
def live_client(live_config: ExecutorConfig) -> Web3Client:
    """Klien RPC sungguhan, dengan timeout dan retry seperti produksi."""
    return Web3Client(live_config.rpc_url)


class TestTheConfiguredEndpointIsBaseSepolia:
    def test_the_chain_id_matches(
        self, live_client: Web3Client, live_config: ExecutorConfig
    ) -> None:
        assert live_client.chain_id() == live_config.chain_id == 84532

    def test_the_host_is_printable_without_leaking_a_key(self, live_config: ExecutorConfig) -> None:
        """Penjaga untuk log: hanya skema dan host yang boleh muncul."""
        aman = endpoint_host(live_config.rpc_url)
        assert aman.startswith(("http://", "https://"))
        assert "?" not in aman
        assert aman.count("/") == 2


class TestTheConfiguredTokenIsUsdc:
    def test_decimals_is_six(self, live_client: Web3Client, live_config: ExecutorConfig) -> None:
        """Kontrak dengan desimal lain membuat setiap jumlah berarti lain."""
        assert live_client.token_decimals(live_config.usdc_address) == REQUIRED_DECIMALS


class TestStartupAgainstTheRealNetwork:
    def test_startup_checks_pass(
        self, live_client: Web3Client, live_config: ExecutorConfig
    ) -> None:
        from payment_guard.chain import load_wallet

        wallet = load_wallet(live_config.wallet_key_path)
        report = check_startup(
            live_client,
            expected_chain_id=live_config.chain_id,
            usdc_address=live_config.usdc_address,
            wallet_address=wallet.address,
            minimum_balance_wei=MINIMUM_GAS_BALANCE_WEI,
        )
        assert report.chain_id == 84532
        assert report.decimals == REQUIRED_DECIMALS

    def test_the_wallet_has_gas(self, live_client: Web3Client, live_config: ExecutorConfig) -> None:
        """Gagal di sini berarti isi faucet, bukan ada yang salah di kode.

        Pesannya menyebut alamatnya supaya operator bisa langsung menyalinnya
        ke faucet; lihat ``scripts/demo_execute.md`` langkah 3.
        """
        from payment_guard.chain import load_wallet

        wallet = load_wallet(live_config.wallet_key_path)
        balance = live_client.eth_balance(wallet.address)
        assert balance >= MINIMUM_GAS_BALANCE_WEI, (
            f"saldo ETH {wallet.address} hanya {balance} wei, di bawah "
            f"{MINIMUM_GAS_BALANCE_WEI}. Isi lewat faucet testnet."
        )
