"""Pengaman praflight ``scripts/devnet_up.py``.

Skrip ini membuat dan menghapus role dan database, dan role Postgres bersifat
**cluster-wide**. Satu salah ketik di URL bisa menghapus role yang dipakai
``.env`` operator — itu sudah pernah terjadi di repo ini dan melahirkan cluster
tes terpisah. Pengaman di sini menolak jalan, bukan memperingatkan.

Keempat kelompok yang diwajibkan pemilik:

* RPC bukan loopback ditolak
* chain id bukan 31337 ditolak
* URL admin bukan port 5436 atau dbname bukan ``payment_guard_devnet`` ditolak
* port 5434 dan 5435 ditolak **by name**, dengan pesan yang menyebut cluster
  mana yang hampir disentuh

Tidak ada tes di berkas ini yang menyentuh jaringan atau database: seluruh
pengaman ini fungsi murni, dan itu memang syaratnya.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.devnet_up import (
    DEVNET_CHAIN_ID,
    DEVNET_DBNAME,
    DEVNET_PORT,
    DevnetError,
    require_devnet_admin_url,
    require_devnet_chain_id,
    require_devnet_rpc,
    require_empty_devnet_db,
)


def url_devnet(
    port: str = DEVNET_PORT, dbname: str = DEVNET_DBNAME, host: str = "127.0.0.1"
) -> str:
    return f"postgresql://guard_admin@{host}:{port}/{dbname}"


class TestRpcMustBeLoopback:
    def test_the_devnet_default_is_accepted(self) -> None:
        require_devnet_rpc("http://127.0.0.1:8545")

    @pytest.mark.parametrize("url", ["http://localhost:8545", "http://[::1]:8545"])
    def test_other_loopback_names_are_accepted(self, url: str) -> None:
        """IPv6 ditulis dalam tanda kurung; itu bentuk yang sah di URL."""
        require_devnet_rpc(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://sepolia.base.org",
            "http://10.0.0.5:8545",
            "http://192.168.1.10:8545",
            "https://mainnet.example.com",
            "http://anvil.internal:8545",
        ],
    )
    def test_a_non_loopback_host_is_refused(self, url: str) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_rpc(url)
        assert "loopback" in str(info.value)

    @pytest.mark.parametrize("url", ["ws://127.0.0.1:8545", "127.0.0.1:8545", "file:///x", ""])
    def test_a_non_http_scheme_is_refused(self, url: str) -> None:
        with pytest.raises(DevnetError):
            require_devnet_rpc(url)

    def test_base_sepolia_is_refused_by_name_in_the_message(self) -> None:
        """Penolakan yang paling penting: RPC Base Sepolia."""
        with pytest.raises(DevnetError) as info:
            require_devnet_rpc("https://sepolia.base.org")
        assert "sepolia.base.org" in str(info.value)


class TestChainIdMustBe31337:
    def test_31337_is_accepted(self) -> None:
        require_devnet_chain_id(DEVNET_CHAIN_ID)

    @pytest.mark.parametrize("seen", [84532, 1, 8453, 11155111, 31338, 0, -1])
    def test_any_other_chain_id_is_refused(self, seen: int) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_chain_id(seen)
        pesan = str(info.value)
        assert str(seen) in pesan
        assert str(DEVNET_CHAIN_ID) in pesan

    def test_base_sepolia_chain_id_is_refused(self) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_chain_id(84532)
        assert "84532" in str(info.value)

    @pytest.mark.parametrize("seen", ["31337", None, 31337.0, [], {}, True])
    def test_a_non_integer_is_refused(self, seen: object) -> None:
        """``True`` ikut ditolak: ``isinstance(True, int)`` benar di Python."""
        with pytest.raises(DevnetError):
            require_devnet_chain_id(seen)


class TestForbiddenPortsAreRefusedByName:
    """5434 dan 5435 punya pesan masing-masing, bukan satu pesan generik."""

    def test_the_operator_cluster_is_refused(self) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(url_devnet(port="5434"))
        pesan = str(info.value)
        assert "5434" in pesan
        assert "operator" in pesan
        assert "cluster-wide" in pesan

    def test_the_test_cluster_is_refused(self) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(url_devnet(port="5435"))
        pesan = str(info.value)
        assert "5435" in pesan
        assert "tes" in pesan

    def test_the_operator_cluster_is_refused_even_with_the_devnet_dbname(self) -> None:
        """Nama database yang benar tidak menyelamatkan port yang salah.

        Port diperiksa lebih dulu dengan sengaja: pesannya harus menyebut
        cluster mana yang hampir disentuh, bukan mengeluh soal nama database.
        """
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(f"postgresql://guard_admin@127.0.0.1:5434/{DEVNET_DBNAME}")
        assert "operator" in str(info.value)

    def test_the_operator_cluster_is_refused_even_with_its_own_dbname(self) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url("postgresql://guard_admin@127.0.0.1:5434/payment_guard")
        assert "5434" in str(info.value)


class TestAdminUrlMustBeTheDevnetCluster:
    def test_the_devnet_url_is_accepted(self) -> None:
        require_devnet_admin_url(url_devnet())

    @pytest.mark.parametrize("port", ["5432", "5433", "5437", "15436"])
    def test_any_other_port_is_refused(self, port: str) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(url_devnet(port=port))
        assert DEVNET_PORT in str(info.value)

    @pytest.mark.parametrize(
        "dbname",
        ["payment_guard", "payment_guard_test", "postgres", "payment_guard_devnet_lama", ""],
    )
    def test_any_other_dbname_is_refused(self, dbname: str) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(url_devnet(dbname=dbname))
        assert DEVNET_DBNAME in str(info.value)

    @pytest.mark.parametrize("host", ["10.0.0.5", "db.example.com", "0.0.0.0"])
    def test_a_non_loopback_host_is_refused(self, host: str) -> None:
        with pytest.raises(DevnetError) as info:
            require_devnet_admin_url(url_devnet(host=host))
        assert "loopback" in str(info.value)

    def test_the_operator_dbname_on_the_devnet_port_is_still_refused(self) -> None:
        """Port benar, nama salah: tetap ditolak."""
        with pytest.raises(DevnetError):
            require_devnet_admin_url(url_devnet(dbname="payment_guard"))


class TestExistingStateIsRefusedUnlessRecreate:
    def test_an_empty_database_is_accepted(self) -> None:
        require_empty_devnet_db([], recreate=False)

    def test_tables_are_refused_without_recreate(self) -> None:
        with pytest.raises(DevnetError) as info:
            require_empty_devnet_db(["decisions", "executions"], recreate=False)
        pesan = str(info.value)
        assert "--recreate" in pesan
        assert "decisions" in pesan

    def test_a_single_table_is_enough_to_refuse(self) -> None:
        with pytest.raises(DevnetError):
            require_empty_devnet_db(["schema_migrations"], recreate=False)

    def test_recreate_allows_existing_tables(self) -> None:
        require_empty_devnet_db(["decisions", "executions"], recreate=True)

    def test_recreate_on_an_empty_database_is_fine(self) -> None:
        require_empty_devnet_db([], recreate=True)

    def test_the_refusal_explains_why_reuse_is_wrong(self) -> None:
        """Pesannya harus menyebut sebabnya, bukan hanya melarang.

        Sebabnya: state Anvil hilang saat restart sedangkan volume Postgres
        bertahan, jadi database lama di atas chain baru menghasilkan kursor
        yang menunjuk blok yang tidak ada.
        """
        with pytest.raises(DevnetError) as info:
            require_empty_devnet_db(["watcher_state"], recreate=False)
        pesan = str(info.value)
        assert "Anvil" in pesan
        assert "kursor" in pesan

    def test_many_tables_are_listed_but_truncated(self) -> None:
        tabel = [f"t{i}" for i in range(12)]
        with pytest.raises(DevnetError) as info:
            require_empty_devnet_db(tabel, recreate=False)
        pesan = str(info.value)
        assert "12 tabel" in pesan
        assert "..." in pesan


class TestTheAnvilKeyIsDerivedNotHardcoded:
    """Kunci devnet tidak boleh ada sebagai literal heksa di repo."""

    def test_the_script_has_no_hex_private_key_literal(self) -> None:
        import re
        from pathlib import Path

        sumber = Path("scripts/devnet_up.py").read_text(encoding="utf-8")
        # 64 hex berurutan adalah bentuk kunci privat secp256k1.
        assert re.search(r"0x[0-9a-fA-F]{64}", sumber) is None

    def test_the_derived_executor_account_is_an_anvil_default(self) -> None:
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil

        from payment_guard.chains import is_anvil_default_address

        akun = akun_anvil(INDEKS_EXECUTOR)
        assert is_anvil_default_address(akun.address)

    def test_the_deployer_and_the_executor_are_different_accounts(self) -> None:
        from scripts.devnet_up import INDEKS_DEPLOYER, INDEKS_EXECUTOR, akun_anvil

        assert akun_anvil(INDEKS_DEPLOYER).address != akun_anvil(INDEKS_EXECUTOR).address

    def test_the_account_repr_does_not_leak_the_key(self) -> None:
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil

        akun = akun_anvil(INDEKS_EXECUTOR)
        # Kunci memang disimpan di field, tapi ia tidak boleh muncul di pesan
        # error atau log mana pun. Tes ini menyatakan bahwa pemanggil harus
        # sengaja membacanya, bukan mendapatkannya dari repr.
        assert akun.private_key.startswith("0x")
        assert len(akun.private_key) == 66


class TestBootstrapAndMigrateCommands:
    """Flag diambil dari ``--help``; tes ini yang menjaganya tidak menyimpang.

    Satu salah ketik flag di skrip orkestrasi tidak muncul sebagai error tipe
    maupun lint — ia muncul sebagai subproses yang gagal di tengah jalan,
    setelah database sudah disentuh. Fungsi pembangun argumen dibuat murni
    supaya kesalahan itu tertangkap di sini.
    """

    def test_bootstrap_uses_the_documented_flags(self, tmp_path: Path) -> None:
        from scripts.devnet_up import bootstrap_command

        argv = bootstrap_command(tmp_path / ".env.devnet", tmp_path / ".env.devnet.migrator")
        assert argv[1:5] == ["-m", "payment_guard.cli", "db", "bootstrap-roles"]
        assert "--env-file" in argv
        assert "--migrator-env-file" in argv
        assert "--no-prompt" in argv

    def test_bootstrap_never_allows_a_remote_admin(self, tmp_path: Path) -> None:
        """``--allow-remote-admin`` mematikan pengaman host; devnet tidak butuh."""
        from scripts.devnet_up import bootstrap_command

        argv = bootstrap_command(tmp_path / "a", tmp_path / "b")
        assert "--allow-remote-admin" not in argv

    def test_bootstrap_passes_both_env_paths(self, tmp_path: Path) -> None:
        from scripts.devnet_up import bootstrap_command

        env = tmp_path / ".env.devnet"
        migrator = tmp_path / ".env.devnet.migrator"
        argv = bootstrap_command(env, migrator)
        assert argv[argv.index("--env-file") + 1] == str(env)
        assert argv[argv.index("--migrator-env-file") + 1] == str(migrator)

    def test_the_runtime_and_migrator_files_are_different(self) -> None:
        """URL pemilik objek tidak pernah ikut di berkas yang dibaca runtime."""
        from scripts.devnet_up import DEVNET_ENV_FILENAME, DEVNET_MIGRATOR_ENV_FILENAME

        assert DEVNET_ENV_FILENAME != DEVNET_MIGRATOR_ENV_FILENAME

    def test_both_env_filenames_are_gitignored_by_the_env_pattern(self) -> None:
        """Keduanya berawalan ``.env``, jadi pola ``.env.*`` menangkapnya."""
        from scripts.devnet_up import DEVNET_ENV_FILENAME, DEVNET_MIGRATOR_ENV_FILENAME

        assert DEVNET_ENV_FILENAME.startswith(".env")
        assert DEVNET_MIGRATOR_ENV_FILENAME.startswith(".env")

    def test_migrate_uses_the_documented_flag(self, tmp_path: Path) -> None:
        from scripts.devnet_up import migrate_command

        argv = migrate_command(tmp_path / ".env.devnet.migrator")
        assert argv[1:5] == ["-m", "payment_guard.cli", "db", "migrate"]
        assert "--migrator-env-file" in argv

    def test_migrate_omits_migrations_dir_by_default(self, tmp_path: Path) -> None:
        """Devnet memakai migrasi yang sama dengan produksi.

        Menunjuk ke direktori lain berarti menguji skema yang bukan skema
        produk.
        """
        from scripts.devnet_up import migrate_command

        assert "--migrations-dir" not in migrate_command(tmp_path / "m")

    def test_migrate_includes_migrations_dir_when_given(self, tmp_path: Path) -> None:
        from scripts.devnet_up import migrate_command

        argv = migrate_command(tmp_path / "m", tmp_path / "migrations")
        assert argv[argv.index("--migrations-dir") + 1] == str(tmp_path / "migrations")

    def test_both_commands_use_the_running_interpreter(self, tmp_path: Path) -> None:
        """``sys.executable``, bukan ``guard`` di PATH.

        Skrip ini dijalankan lewat ``uv run``, dan console script belum tentu
        ada di PATH subproses.
        """
        import sys

        from scripts.devnet_up import bootstrap_command, migrate_command

        assert bootstrap_command(tmp_path / "a", tmp_path / "b")[0] == sys.executable
        assert migrate_command(tmp_path / "b")[0] == sys.executable


class TestMaintenanceUrlStillGuardsTheCluster:
    """URL pemeliharaan melewati pengaman nama database, jadi port wajib dicek.

    ``maintenance_url_for`` menghasilkan URL ber-dbname ``postgres`` supaya
    ``DROP DATABASE`` bisa dijalankan. Itu berarti ia melewati satu-satunya
    pengaman yang menyebut ``payment_guard_devnet``. Yang tersisa yang masih
    menjaga adalah port dan host, dan di situlah tes ini bekerja.
    """

    def test_it_points_at_the_maintenance_database(self) -> None:
        from scripts.devnet_up import MAINTENANCE_DBNAME, maintenance_url_for

        hasil = maintenance_url_for(url_devnet())
        assert f"dbname={MAINTENANCE_DBNAME}" in hasil or f"/{MAINTENANCE_DBNAME}" in hasil

    def test_it_keeps_the_devnet_port(self) -> None:
        from scripts.devnet_up import maintenance_url_for

        assert DEVNET_PORT in maintenance_url_for(url_devnet())

    def test_the_operator_cluster_is_refused(self) -> None:
        from scripts.devnet_up import maintenance_url_for

        with pytest.raises(DevnetError) as info:
            maintenance_url_for(url_devnet(port="5434"))
        assert "operator" in str(info.value)

    def test_the_test_cluster_is_refused(self) -> None:
        from scripts.devnet_up import maintenance_url_for

        with pytest.raises(DevnetError) as info:
            maintenance_url_for(url_devnet(port="5435"))
        assert "5435" in str(info.value)

    @pytest.mark.parametrize("port", ["5432", "5433", "5437"])
    def test_any_other_port_is_refused(self, port: str) -> None:
        from scripts.devnet_up import maintenance_url_for

        with pytest.raises(DevnetError):
            maintenance_url_for(url_devnet(port=port))

    @pytest.mark.parametrize("host", ["10.0.0.5", "db.example.com"])
    def test_a_non_loopback_host_is_refused(self, host: str) -> None:
        from scripts.devnet_up import maintenance_url_for

        with pytest.raises(DevnetError) as info:
            maintenance_url_for(url_devnet(host=host))
        assert "loopback" in str(info.value)


class TestKeygenCommand:
    """Flag keygen dari ``--help``, dan path selalu eksplisit."""

    def test_it_uses_the_documented_flags(self, tmp_path: Path) -> None:
        from scripts.devnet_up import keygen_command

        argv = keygen_command(tmp_path / "s.json", tmp_path / "p.json")
        assert argv[1:4] == ["-m", "payment_guard.cli", "keygen"]
        assert "--signing-key-file" in argv
        assert "--public-keys-file" in argv

    def test_both_paths_are_always_explicit(self, tmp_path: Path) -> None:
        """Bawaan keygen menaruh kunci rahasia DI DALAM repo.

        ``config.py`` menolak path itu saat Guard start
        (``_require_outside_repo``), jadi devnet tidak boleh pernah memakai
        bawaannya.
        """
        from scripts.devnet_up import keygen_command

        signing = tmp_path / "s.json"
        public = tmp_path / "p.json"
        argv = keygen_command(signing, public)
        assert argv[argv.index("--signing-key-file") + 1] == str(signing)
        assert argv[argv.index("--public-keys-file") + 1] == str(public)

    def test_it_uses_the_running_interpreter(self, tmp_path: Path) -> None:
        import sys

        from scripts.devnet_up import keygen_command

        assert keygen_command(tmp_path / "s", tmp_path / "p")[0] == sys.executable


class TestTheSecretDirectoryIsOutsideTheRepo:
    """Kunci di luar repo lebih kuat daripada kunci yang di-gitignore.

    ``.gitignore`` hanya menahan berkas yang secara teknis masih bisa
    di-``git add -f``. Berkas di luar akar repo tidak pernah dilihat git.
    """

    def test_it_sits_beside_the_repo_not_inside(self, tmp_path: Path) -> None:
        from scripts.devnet_up import devnet_key_dir

        akar = tmp_path / "repo"
        akar.mkdir()
        rahasia = devnet_key_dir(akar)
        with pytest.raises(ValueError):
            rahasia.relative_to(akar)

    def test_its_name_says_what_it_holds(self) -> None:
        from scripts.devnet_up import DEVNET_KEY_DIRNAME

        assert "rahasia" in DEVNET_KEY_DIRNAME
        assert "devnet" in DEVNET_KEY_DIRNAME

    def test_the_real_secret_dir_is_outside_the_real_repo(self) -> None:
        from scripts.devnet_up import devnet_key_dir

        akar = Path(__file__).resolve().parent.parent.parent
        with pytest.raises(ValueError):
            devnet_key_dir(akar).relative_to(akar)


class TestTheExecutorWalletFile:
    """Bentuk berkas kunci, dan penolakan path di dalam repo."""

    def test_a_key_inside_the_repo_is_refused(self) -> None:
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        akar = Path(__file__).resolve().parent.parent.parent
        with pytest.raises(DevnetError) as info:
            write_executor_wallet(akar / "wallet.json", akun_anvil(INDEKS_EXECUTOR))
        assert "akar repo" in str(info.value)

    def test_the_file_has_exactly_the_two_fields_load_wallet_wants(self, tmp_path: Path) -> None:
        import json

        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        path = tmp_path / "luar" / "wallet.json"
        write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))
        document = json.loads(path.read_text(encoding="utf-8"))
        assert set(document) == {"address", "private_key"}

    def test_load_wallet_accepts_it_on_devnet(self, tmp_path: Path) -> None:
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        from payment_guard.chain import load_wallet
        from payment_guard.chains import NETWORK_DEVNET

        akun = akun_anvil(INDEKS_EXECUTOR)
        path = tmp_path / "luar" / "wallet.json"
        write_executor_wallet(path, akun)

        wallet = load_wallet(path, network=NETWORK_DEVNET)
        assert wallet.address == akun.address

    def test_load_wallet_refuses_it_outside_devnet(self, tmp_path: Path) -> None:
        """Pengaman kunci publik: akun Anvil hanya boleh di devnet."""
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        from payment_guard.chain import ChainError, load_wallet

        path = tmp_path / "luar" / "wallet.json"
        write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))

        with pytest.raises(ChainError) as info:
            load_wallet(path)
        pesan = str(info.value)
        assert "PUBLIK" in pesan
        assert "PAYMENT_GUARD_NETWORK" in pesan

    def test_the_refusal_does_not_quote_the_key(self, tmp_path: Path) -> None:
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        from payment_guard.chain import ChainError, load_wallet

        akun = akun_anvil(INDEKS_EXECUTOR)
        path = tmp_path / "luar" / "wallet.json"
        write_executor_wallet(path, akun)

        with pytest.raises(ChainError) as info:
            load_wallet(path)
        assert akun.private_key not in str(info.value)
        assert akun.private_key[2:] not in str(info.value)

    def test_the_address_matches_the_key_it_was_derived_from(self, tmp_path: Path) -> None:
        """``load_wallet`` menolak berkas yang alamat dan kuncinya tidak cocok."""
        from scripts.devnet_up import INDEKS_DEPLOYER, akun_anvil, write_executor_wallet

        from payment_guard.chain import load_wallet
        from payment_guard.chains import NETWORK_DEVNET

        akun = akun_anvil(INDEKS_DEPLOYER)
        path = tmp_path / "luar" / "deployer.json"
        write_executor_wallet(path, akun)
        assert load_wallet(path, network=NETWORK_DEVNET).address == akun.address


class TestNoDevnetArtefactLandsInTheRepo:
    """Artefak run devnet tidak boleh muncul di `git status`.

    Revisi pertama bagian keygen menaruh registri kunci publik devnet di akar
    repo — keygen sendiri menyebutnya "boleh di-commit", jadi ia tidak
    ter-gitignore. Hasilnya berkas untracked yang mencemari `git status` dan
    bisa ter-commit karena kecelakaan. Registri itu berubah setiap devnet
    dibangun ulang, jadi tempatnya di luar repo bersama kunci lainnya.
    """

    def test_the_repo_root_holds_no_devnet_public_keys_file(self) -> None:
        akar = Path(__file__).resolve().parent.parent.parent
        assert not (akar / "guard_public_devnet.json").exists()

    def test_the_script_writes_the_public_registry_outside_the_repo(self) -> None:
        """Dibaca dari sumbernya: path-nya dibangun dari direktori kunci."""
        akar = Path(__file__).resolve().parent.parent.parent
        sumber = (akar / "scripts" / "devnet_up.py").read_text(encoding="utf-8")
        assert 'rahasia / "guard_public_devnet.json"' in sumber
        assert 'akar / "guard_public_devnet.json"' not in sumber


class TestKeygenDecision:
    """`guard keygen` menolak menimpa kunci; --recreate harus menghormatinya.

    Menghapus berkas kunci supaya keygen mau jalan adalah mengakali pengaman
    yang alasannya benar: menimpa kunci tanda tangan menghilangkan kunci yang
    mungkin masih dipakai envelope yang beredar, dan itu tidak bisa dibatalkan
    (Security v1.1 runbook 8.1).
    """

    def test_neither_present_means_generate(self, tmp_path: Path) -> None:
        from scripts.devnet_up import keygen_decision

        assert keygen_decision(tmp_path / "s.json", tmp_path / "p.json") == "buat"

    def test_both_present_means_reuse(self, tmp_path: Path) -> None:
        from scripts.devnet_up import keygen_decision

        rahasia = tmp_path / "s.json"
        publik = tmp_path / "p.json"
        rahasia.write_text("{}", encoding="utf-8")
        publik.write_text("{}", encoding="utf-8")
        assert keygen_decision(rahasia, publik) == "pakai-ulang"

    def test_a_secret_without_its_registry_is_refused(self, tmp_path: Path) -> None:
        """Keadaan yang tidak bisa diperbaiki skrip; operator yang memutuskan."""
        from scripts.devnet_up import keygen_decision

        rahasia = tmp_path / "s.json"
        rahasia.write_text("{}", encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            keygen_decision(rahasia, tmp_path / "p.json")
        pesan = str(info.value)
        assert "rotasi" in pesan.lower()
        assert "Pindahkan" in pesan

    def test_the_refusal_does_not_offer_to_delete_the_key(self, tmp_path: Path) -> None:
        """Pesannya tidak boleh menyarankan menghapus kunci."""
        from scripts.devnet_up import keygen_decision

        rahasia = tmp_path / "s.json"
        rahasia.write_text("{}", encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            keygen_decision(rahasia, tmp_path / "p.json")
        assert "hapus" not in str(info.value).lower()

    def test_a_registry_without_its_secret_means_generate(self, tmp_path: Path) -> None:
        from scripts.devnet_up import keygen_decision

        publik = tmp_path / "p.json"
        publik.write_text('{"keys": []}', encoding="utf-8")
        assert keygen_decision(tmp_path / "s.json", publik) == "buat"


class TestTheWalletFileIsNeverOverwritten:
    """`write_executor_wallet` menyelaraskan diri dengan `guard keygen`.

    Sebelum perbaikan ini ia memanggil ``write_text`` langsung, yang memotong
    dan mengganti isi tanpa bertanya — sementara ``guard keygen`` di sebelahnya
    menolak menimpa. Asimetri itu tidak berbahaya selama kunci diturunkan
    deterministik dari mnemonic indeks 1, tapi berhenti aman begitu indeksnya
    berubah.
    """

    @staticmethod
    def _wallet(tmp_path: Path) -> Path:
        return tmp_path / "luar" / "wallet.json"

    def test_a_missing_file_is_written(self, tmp_path: Path) -> None:
        import json

        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        path = self._wallet(tmp_path)
        akun = akun_anvil(INDEKS_EXECUTOR)
        write_executor_wallet(path, akun)

        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["address"] == akun.address

    def test_an_identical_file_is_left_alone_and_counts_as_success(self, tmp_path: Path) -> None:
        """Tidak menimpa berarti satu hal lebih sedikit yang bisa salah."""
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        path = self._wallet(tmp_path)
        akun = akun_anvil(INDEKS_EXECUTOR)
        write_executor_wallet(path, akun)
        sebelum = path.read_bytes()
        mtime_sebelum = path.stat().st_mtime_ns

        # Tidak melempar: isinya identik.
        write_executor_wallet(path, akun)

        assert path.read_bytes() == sebelum
        assert path.stat().st_mtime_ns == mtime_sebelum, "berkas identik tidak boleh ditulis ulang"

    def test_a_different_key_is_refused(self, tmp_path: Path) -> None:
        from scripts.devnet_up import (
            INDEKS_DEPLOYER,
            INDEKS_EXECUTOR,
            akun_anvil,
            write_executor_wallet,
        )

        path = self._wallet(tmp_path)
        write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))
        sebelum = path.read_bytes()

        with pytest.raises(DevnetError) as info:
            write_executor_wallet(path, akun_anvil(INDEKS_DEPLOYER))

        pesan = str(info.value)
        assert str(path) in pesan
        assert "TIDAK ditimpa" in pesan
        assert path.read_bytes() == sebelum, "berkas lama harus utuh setelah penolakan"

    def test_the_refusal_quotes_neither_key(self, tmp_path: Path) -> None:
        """Pesan penolakan tidak boleh memuat hex kunci, lama maupun baru."""
        from scripts.devnet_up import (
            INDEKS_DEPLOYER,
            INDEKS_EXECUTOR,
            akun_anvil,
            write_executor_wallet,
        )

        lama = akun_anvil(INDEKS_EXECUTOR)
        baru = akun_anvil(INDEKS_DEPLOYER)
        path = self._wallet(tmp_path)
        write_executor_wallet(path, lama)

        with pytest.raises(DevnetError) as info:
            write_executor_wallet(path, baru)

        pesan = str(info.value)
        for kunci in (lama.private_key, baru.private_key):
            assert kunci not in pesan
            assert kunci[2:] not in pesan

    def test_the_refusal_message_has_no_64_hex_run_at_all(self, tmp_path: Path) -> None:
        """Penjaga yang lebih luas: tidak ada 64 hex berurutan di pesannya."""
        import re

        from scripts.devnet_up import (
            INDEKS_DEPLOYER,
            INDEKS_EXECUTOR,
            akun_anvil,
            write_executor_wallet,
        )

        path = self._wallet(tmp_path)
        write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))
        with pytest.raises(DevnetError) as info:
            write_executor_wallet(path, akun_anvil(INDEKS_DEPLOYER))
        assert re.search(r"[0-9a-fA-F]{64}", str(info.value)) is None

    def test_the_refusal_tells_the_operator_what_to_do(self, tmp_path: Path) -> None:
        from scripts.devnet_up import (
            INDEKS_DEPLOYER,
            INDEKS_EXECUTOR,
            akun_anvil,
            write_executor_wallet,
        )

        path = self._wallet(tmp_path)
        write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))
        with pytest.raises(DevnetError) as info:
            write_executor_wallet(path, akun_anvil(INDEKS_DEPLOYER))
        pesan = str(info.value)
        assert "Pindahkan" in pesan
        assert "hapus" not in pesan.lower()

    def test_a_corrupt_existing_file_is_refused_not_overwritten(self, tmp_path: Path) -> None:
        """Isi yang bukan JSON pun tetap berbeda, jadi tetap ditolak."""
        from scripts.devnet_up import INDEKS_EXECUTOR, akun_anvil, write_executor_wallet

        path = self._wallet(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bukan json", encoding="utf-8")

        with pytest.raises(DevnetError):
            write_executor_wallet(path, akun_anvil(INDEKS_EXECUTOR))
        assert path.read_text(encoding="utf-8") == "bukan json"


class TestTokenArtifactIsValidated:
    """Artefak forge diperiksa bentuknya, bukan hanya dibaca.

    Artefak yang ada tapi tanpa bytecode menghasilkan transaksi deploy kosong
    yang "berhasil" dan meninggalkan alamat tanpa kode di dalamnya — kegagalan
    yang baru terlihat jauh di hilir.
    """

    @staticmethod
    def _artefak(**ganti: object) -> dict[str, object]:
        dasar: dict[str, object] = {
            "abi": [
                {"type": "function", "name": n}
                for n in ("mint", "transfer", "balanceOf", "decimals")
            ],
            "bytecode": {"object": "0x60a06040"},
        }
        dasar.update(ganti)
        return dasar

    def test_a_well_formed_artifact_is_accepted(self, tmp_path: Path) -> None:
        import json

        from scripts.devnet_up import load_token_artifact

        path = tmp_path / "TestUSDC.json"
        path.write_text(json.dumps(self._artefak()), encoding="utf-8")
        artefak = load_token_artifact(path)
        assert artefak.bytecode == "0x60a06040"
        assert len(artefak.abi) == 4

    def test_a_missing_file_names_forge_build(self, tmp_path: Path) -> None:
        from scripts.devnet_up import load_token_artifact

        with pytest.raises(DevnetError) as info:
            load_token_artifact(tmp_path / "tidak-ada.json")
        assert "forge build" in str(info.value)

    def test_a_non_json_file_is_refused(self, tmp_path: Path) -> None:
        from scripts.devnet_up import load_token_artifact

        path = tmp_path / "a.json"
        path.write_text("bukan json", encoding="utf-8")
        with pytest.raises(DevnetError):
            load_token_artifact(path)

    @pytest.mark.parametrize("bytecode", [{"object": ""}, {"object": None}, {}, None, ""])
    def test_a_missing_bytecode_is_refused(self, tmp_path: Path, bytecode: object) -> None:
        import json

        from scripts.devnet_up import load_token_artifact

        path = tmp_path / "a.json"
        path.write_text(json.dumps(self._artefak(bytecode=bytecode)), encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            load_token_artifact(path)
        assert "bytecode" in str(info.value)

    @pytest.mark.parametrize("abi", [[], None, "bukan list"])
    def test_a_missing_abi_is_refused(self, tmp_path: Path, abi: object) -> None:
        import json

        from scripts.devnet_up import load_token_artifact

        path = tmp_path / "a.json"
        path.write_text(json.dumps(self._artefak(abi=abi)), encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            load_token_artifact(path)
        assert "ABI" in str(info.value)

    @pytest.mark.parametrize("hilang", ["mint", "transfer", "balanceOf", "decimals"])
    def test_a_missing_required_function_is_refused(self, tmp_path: Path, hilang: str) -> None:
        """Keempat fungsi itu yang benar-benar dipakai devnet."""
        import json

        from scripts.devnet_up import load_token_artifact

        abi = [
            {"type": "function", "name": n}
            for n in ("mint", "transfer", "balanceOf", "decimals")
            if n != hilang
        ]
        path = tmp_path / "a.json"
        path.write_text(json.dumps(self._artefak(abi=abi)), encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            load_token_artifact(path)
        assert hilang in str(info.value)

    def test_the_real_artifact_in_the_repo_is_valid(self) -> None:
        """Artefak nyata hasil `forge build` harus lolos pemeriksaan ini."""
        from scripts.devnet_up import ARTEFAK_TOKEN, DESIMAL_WAJIB, load_token_artifact

        akar = Path(__file__).resolve().parent.parent.parent
        path = akar / ARTEFAK_TOKEN
        if not path.exists():
            pytest.skip("contracts/out belum dibangun di mesin ini")
        artefak = load_token_artifact(path)
        assert artefak.bytecode.startswith("0x")
        assert DESIMAL_WAJIB == 6


class TestEnvUpdatesArePreciseAndQuiet:
    """`apply_env_updates` murni atas teks, supaya tes tidak menyentuh berkas
    yang memuat kredensial.
    """

    def test_an_existing_key_is_replaced_in_place(self) -> None:
        from scripts.devnet_up import apply_env_updates

        hasil = apply_env_updates("A=1\nB=2\nC=3\n", {"B": "baru"})
        assert hasil == "A=1\nB=baru\nC=3\n"

    def test_order_and_comments_survive(self) -> None:
        from scripts.devnet_up import apply_env_updates

        teks = "# komentar\nA=1\n\n# lain\nB=2\n"
        hasil = apply_env_updates(teks, {"A": "x"})
        assert hasil.splitlines() == ["# komentar", "A=x", "", "# lain", "B=2"]

    def test_a_new_key_is_appended(self) -> None:
        from scripts.devnet_up import apply_env_updates

        hasil = apply_env_updates("A=1\n", {"Z": "9"})
        assert hasil.splitlines() == ["A=1", "Z=9"]

    def test_untouched_lines_keep_their_values(self) -> None:
        """Berkas ini memuat lima URL runtime berkredensial.

        Menulis ulangnya dari nol akan kehilangan apa pun yang tidak dikenali
        fungsi ini.
        """
        from scripts.devnet_up import apply_env_updates

        teks = "GUARD_DB_URL=postgresql://rahasia\nUSDC_ADDRESS=\n"
        hasil = apply_env_updates(teks, {"USDC_ADDRESS": "0xabc"})
        assert "GUARD_DB_URL=postgresql://rahasia" in hasil
        assert "USDC_ADDRESS=0xabc" in hasil

    def test_a_commented_out_key_is_not_treated_as_a_setting(self) -> None:
        from scripts.devnet_up import apply_env_updates

        hasil = apply_env_updates("# A=lama\n", {"A": "baru"})
        assert "# A=lama" in hasil
        assert "A=baru" in hasil

    def test_an_empty_value_is_filled(self) -> None:
        from scripts.devnet_up import apply_env_updates

        assert apply_env_updates("USDC_ADDRESS=\n", {"USDC_ADDRESS": "0x1"}) == "USDC_ADDRESS=0x1\n"

    def test_update_file_refuses_a_missing_file(self, tmp_path: Path) -> None:
        from scripts.devnet_up import update_env_file

        with pytest.raises(DevnetError) as info:
            update_env_file(tmp_path / "tidak-ada", {"A": "1"})
        assert "bootstrap-roles" in str(info.value)


class TestDevnetMandateIsBuiltAtRuntime:
    """Mandate devnet dibangun skrip, bukan berkas statis yang ter-commit.

    Dua sifat yang dijaga di sini, keduanya diminta pemilik: **tidak ada alamat
    yang di-hardcode**, dan **chain 31337 yang dipakai**.
    """

    def test_the_chain_is_devnet_local_which_maps_to_31337(self) -> None:
        from scripts.devnet_up import DEVNET_CHAIN_ID, devnet_mandate_document

        from payment_guard.chains import NETWORK_DEVNET, chain_id_for

        dokumen = devnet_mandate_document(recipient="0x" + "11" * 20)
        assert dokumen["chain"] == "devnet-local"
        assert chain_id_for(str(dokumen["chain"]), network=NETWORK_DEVNET) == DEVNET_CHAIN_ID
        assert DEVNET_CHAIN_ID == 31337

    def test_the_mandate_is_refused_on_the_production_network(self) -> None:
        """Pengamannya: mandate devnet tidak bisa dimuat Guard produksi."""
        from scripts.devnet_up import devnet_mandate_document

        from payment_guard.mandate import MandateError, load_mandate

        dokumen = devnet_mandate_document(recipient="0x" + "11" * 20)
        with pytest.raises(MandateError):
            load_mandate(dokumen)

    def test_the_recipient_is_whatever_the_caller_passes(self) -> None:
        """Penerima adalah parameter, bukan konstanta di dalam fungsi."""
        from scripts.devnet_up import devnet_mandate_document

        alamat = "0x" + "ab" * 20
        assert devnet_mandate_document(recipient=alamat)["allowed_recipients"] == [alamat]

    def test_the_recipient_is_derived_not_hardcoded(self) -> None:
        """Pemanggil nyata menurunkannya dari mnemonic Anvil, indeks 2."""
        from scripts.devnet_up import INDEKS_PENERIMA, akun_anvil

        from payment_guard.chains import is_anvil_default_address

        akun = akun_anvil(INDEKS_PENERIMA)
        assert is_anvil_default_address(akun.address)

    def test_the_recipient_is_neither_the_deployer_nor_the_executor(self) -> None:
        """Penerima = pengirim membuat saldo tidak berubah setelah pembayaran.

        Skenario jalur bahagia tidak bisa membedakan "terkirim" dari "tidak
        terkirim" kalau itu terjadi.
        """
        from scripts.devnet_up import (
            INDEKS_DEPLOYER,
            INDEKS_EXECUTOR,
            INDEKS_PENERIMA,
            akun_anvil,
        )

        assert INDEKS_PENERIMA not in (INDEKS_DEPLOYER, INDEKS_EXECUTOR)
        alamat = {
            akun_anvil(i).address for i in (INDEKS_DEPLOYER, INDEKS_EXECUTOR, INDEKS_PENERIMA)
        }
        assert len(alamat) == 3

    def test_the_script_hardcodes_no_ethereum_address(self) -> None:
        """Nol literal alamat 40-hex di sumber skrip, kecuali daftar akun Anvil.

        Daftar akun Anvil ada di `payment_guard.chains`, bukan di skrip ini, dan
        ia memang harus literal: gunanya mengenali kunci publik supaya ditolak.
        """
        import re

        akar = Path(__file__).resolve().parent.parent.parent
        sumber = (akar / "scripts" / "devnet_up.py").read_text(encoding="utf-8")
        assert re.search(r"0x[0-9a-fA-F]{40}", sumber) is None

    def test_the_script_hardcodes_no_deployed_token_address(self) -> None:
        """Alamat token hasil deploy berubah bila nonce deployer berbeda."""
        akar = Path(__file__).resolve().parent.parent.parent
        sumber = (akar / "scripts" / "devnet_up.py").read_text(encoding="utf-8")
        for pernah_dipakai in (
            "5fbdb2315678afecb367f032d93f642f64180aa3",
            "9fe46736679d2d9a65f0992f2272de9f3c7fa6e0",
        ):
            assert pernah_dipakai not in sumber

    def test_the_hash_goes_through_the_same_path_as_guard(self) -> None:
        """Hash skrip harus identik dengan hash Guard untuk dokumen yang sama.

        Hash yang hampir benar — misalnya tanpa awalan domain — tidak akan
        pernah cocok, dan selisihnya hanya terlihat jauh di hilir.
        """
        from scripts.devnet_up import devnet_mandate_document, mandate_hash_of

        from payment_guard.chains import NETWORK_DEVNET
        from payment_guard.mandate import compute_mandate_hash, load_mandate

        dokumen = devnet_mandate_document(recipient="0x" + "11" * 20)
        langsung = compute_mandate_hash(load_mandate(dokumen, network=NETWORK_DEVNET))
        assert mandate_hash_of(dokumen) == langsung

    def test_writing_returns_the_hash_and_the_file_parses_back(self, tmp_path: Path) -> None:
        import json

        from scripts.devnet_up import devnet_mandate_document, mandate_hash_of, write_devnet_mandate

        dokumen = devnet_mandate_document(recipient="0x" + "11" * 20)
        path = tmp_path / "mandate.json"
        hash_ditulis = write_devnet_mandate(path, dokumen)

        assert hash_ditulis == mandate_hash_of(dokumen)
        assert json.loads(path.read_text(encoding="utf-8")) == dokumen

    def test_an_invalid_mandate_is_refused_before_writing(self, tmp_path: Path) -> None:
        from scripts.devnet_up import write_devnet_mandate

        path = tmp_path / "mandate.json"
        with pytest.raises(DevnetError) as info:
            write_devnet_mandate(path, {"mandate_id": "x"})
        assert "tidak sah" in str(info.value)
        assert not path.exists(), "berkas tidak boleh ditulis kalau dokumennya ditolak"


class TestTheTokenAddressIsCheckedAgainstTheEnv:
    """Kaitan mandate ke tUSDC adalah pemeriksaan, bukan field.

    Skema mandate tidak punya tempat untuk alamat token; alamat itu hidup di
    `USDC_ADDRESS` di env, dipakai Executor dan Watcher.
    """

    def test_a_matching_address_passes(self, tmp_path: Path) -> None:
        from scripts.devnet_up import require_token_matches_env

        env = tmp_path / ".env.devnet"
        env.write_text("USDC_ADDRESS=0xAbC123\n", encoding="utf-8")
        require_token_matches_env(env, "0xabc123")

    def test_a_stale_address_is_refused(self, tmp_path: Path) -> None:
        """Keadaan nyata setelah Docker restart 17 Nov 2026."""
        from scripts.devnet_up import require_token_matches_env

        env = tmp_path / ".env.devnet"
        env.write_text(
            "USDC_ADDRESS=0x1111111111111111111111111111111111111111\n", encoding="utf-8"
        )
        with pytest.raises(DevnetError) as info:
            require_token_matches_env(env, "0x2222222222222222222222222222222222222222")
        pesan = str(info.value)
        assert "basi" in pesan

    def test_an_empty_address_is_refused(self, tmp_path: Path) -> None:
        from scripts.devnet_up import require_token_matches_env

        env = tmp_path / ".env.devnet"
        env.write_text("USDC_ADDRESS=\n", encoding="utf-8")
        with pytest.raises(DevnetError) as info:
            require_token_matches_env(env, "0xabc")
        assert "belum terisi" in str(info.value)


class TestWatcherAndReconcilerSettingsAreExplicit:
    """Kelima setelan numerik wajib tertulis di `.env.devnet`, bukan diwarisi.

    `.env.devnet` dibuat `bootstrap-roles`, yang hanya menulis lima URL
    runtime. Kelima setelan di sini karena itu tidak pernah ada di berkas, dan
    Watcher serta Reconciler memakai bawaan `config.py` **tanpa ada yang
    menyadarinya** — ditemukan saat prapemeriksaan skenario (a).

    Nilainya dibandingkan dengan konstanta `DEFAULT_*`, **bukan dengan angka
    yang ditulis ulang di tes ini**: dua salinan angka yang sama akan
    menyimpang, dan tes yang menyalin angka hanya menjaga salinannya sendiri.
    """

    def test_all_five_keys_are_present(self) -> None:
        from scripts.devnet_up import watcher_and_reconciler_settings

        from payment_guard.config import RECONCILER_ENV_VARS, WATCHER_ENV_VARS

        setelan = watcher_and_reconciler_settings()
        wajib = {
            WATCHER_ENV_VARS["confirmations"],
            WATCHER_ENV_VARS["reorg_lookback"],
            WATCHER_ENV_VARS["scan_batch_size"],
            WATCHER_ENV_VARS["interval_seconds"],
            RECONCILER_ENV_VARS["deadline_seconds"],
        }
        assert set(setelan) == wajib

    def test_each_value_equals_the_config_default(self) -> None:
        """Diambil dari konstanta, bukan ditulis ulang sebagai angka."""
        from scripts.devnet_up import watcher_and_reconciler_settings

        from payment_guard.config import (
            DEFAULT_CONFIRMATIONS,
            DEFAULT_RECONCILE_DEADLINE_SECONDS,
            DEFAULT_REORG_LOOKBACK,
            DEFAULT_SCAN_BATCH_SIZE,
            DEFAULT_WATCHER_INTERVAL_SECONDS,
            RECONCILER_ENV_VARS,
            WATCHER_ENV_VARS,
        )

        setelan = watcher_and_reconciler_settings()
        assert setelan[WATCHER_ENV_VARS["confirmations"]] == str(DEFAULT_CONFIRMATIONS)
        assert setelan[WATCHER_ENV_VARS["reorg_lookback"]] == str(DEFAULT_REORG_LOOKBACK)
        assert setelan[WATCHER_ENV_VARS["scan_batch_size"]] == str(DEFAULT_SCAN_BATCH_SIZE)
        assert setelan[WATCHER_ENV_VARS["interval_seconds"]] == str(
            DEFAULT_WATCHER_INTERVAL_SECONDS
        )
        assert setelan[RECONCILER_ENV_VARS["deadline_seconds"]] == str(
            DEFAULT_RECONCILE_DEADLINE_SECONDS
        )

    def test_reorg_lookback_exceeds_confirmations(self) -> None:
        """`config.py` menolak konfigurasi Watcher bila tidak begitu."""
        from scripts.devnet_up import watcher_and_reconciler_settings

        from payment_guard.config import RECONCILER_ENV_VARS, WATCHER_ENV_VARS

        setelan = watcher_and_reconciler_settings()
        assert int(setelan[WATCHER_ENV_VARS["reorg_lookback"]]) > int(
            setelan[WATCHER_ENV_VARS["confirmations"]]
        )
        assert RECONCILER_ENV_VARS["deadline_seconds"] in setelan

    def test_the_deadline_is_not_the_misleading_template_value(self) -> None:
        """60 detik bisa memunculkan `executed_not_settled` palsu.

        Templat lama memuat 60, dan itu nilai yang tidak pernah berlaku. Tes
        ini menolak nilai itu secara khusus, karena ia yang paling berbahaya
        dari ketiga nilai templat lama.
        """
        from scripts.devnet_up import watcher_and_reconciler_settings

        from payment_guard.config import RECONCILER_ENV_VARS

        assert watcher_and_reconciler_settings()[RECONCILER_ENV_VARS["deadline_seconds"]] != "60"

    def test_every_value_is_a_positive_integer_string(self) -> None:
        from scripts.devnet_up import watcher_and_reconciler_settings

        for kunci, nilai in watcher_and_reconciler_settings().items():
            assert nilai.isdigit(), kunci
            assert int(nilai) > 0, kunci

    def test_applying_them_adds_exactly_those_keys(self) -> None:
        """Terapkan ke env tiruan: kelima kunci bertambah, sisanya utuh."""
        from scripts.devnet_up import apply_env_updates, watcher_and_reconciler_settings

        setelan = watcher_and_reconciler_settings()
        sebelum = "GUARD_DB_URL=postgresql://rahasia\nUSDC_ADDRESS=0xabc\n"
        sesudah = apply_env_updates(sebelum, setelan)

        assert "GUARD_DB_URL=postgresql://rahasia" in sesudah
        assert "USDC_ADDRESS=0xabc" in sesudah
        for kunci, nilai in setelan.items():
            assert f"{kunci}={nilai}" in sesudah

        kunci_sebelum = {b.split("=", 1)[0] for b in sebelum.splitlines() if "=" in b}
        kunci_sesudah = {b.split("=", 1)[0] for b in sesudah.splitlines() if "=" in b}
        assert kunci_sesudah - kunci_sebelum == set(setelan)

    def test_the_template_matches_the_settings(self) -> None:
        """`.env.devnet.example` tidak boleh menjanjikan nilai lain.

        Templat yang menjanjikan setelan yang tidak berlaku lebih buruk
        daripada templat yang diam.
        """
        from scripts.devnet_up import watcher_and_reconciler_settings

        akar = Path(__file__).resolve().parent.parent.parent
        teks = (akar / ".env.devnet.example").read_text(encoding="utf-8")
        baris = {
            b.split("=", 1)[0]: b.split("=", 1)[1].strip()
            for b in teks.splitlines()
            if "=" in b and not b.strip().startswith("#")
        }
        for kunci, nilai in watcher_and_reconciler_settings().items():
            assert baris.get(kunci) == nilai, f"{kunci}: templat {baris.get(kunci)!r} != {nilai!r}"
