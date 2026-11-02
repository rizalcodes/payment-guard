"""Validasi host URL admin, dan jalur input URL admin.

``bootstrap-roles`` membuat role dan mengubah hak tingkat schema. Menjalankannya
terhadap cluster yang salah adalah kesalahan mahal, dan murah dicegah. Tes di
sini tidak menyentuh database: yang diuji adalah penolakan sebelum satu pun
koneksi dibuka.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payment_guard.bootstrap import (
    admin_hosts,
    is_loopback_host,
    read_admin_url,
    require_loopback_admin,
    run_bootstrap_roles,
)
from payment_guard.config import ADMIN_URL_ENV_VAR
from payment_guard.db import MigrationError

# Canary palsu. Bukan kredensial nyata.
ADMIN_PASSWORD = "p4ssword-admin-palsu"


def admin_url(host: str) -> str:
    return f"postgresql://admin:{ADMIN_PASSWORD}@{host}:5434/payment_guard"


class TestIsLoopbackHost:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.2",  # seluruh 127.0.0.0/8 adalah loopback
            "127.1.2.3",
            "::1",
            "[::1]",
            "localhost",
            "LOCALHOST",
            "LocalHost",
            "/var/run/postgresql",  # socket Unix
            "/tmp",
            "@abstrak",  # socket abstrak Linux
        ],
    )
    def test_local_hosts_are_accepted(self, host: str) -> None:
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.5",
            "192.168.1.10",
            "172.16.0.1",
            "8.8.8.8",
            "0.0.0.0",  # bukan loopback: itu "semua antarmuka"
            "db.internal",
            "prod-postgres.example.com",
            "::",
            "2001:db8::1",
            "127.0.0.1.evil.com",  # awalan menipu
            "localhost.evil.com",
        ],
    )
    def test_remote_hosts_are_rejected(self, host: str) -> None:
        assert is_loopback_host(host) is False


class TestAdminHosts:
    def test_single_host(self) -> None:
        assert admin_hosts(admin_url("127.0.0.1")) == ["127.0.0.1"]

    def test_keyword_form(self) -> None:
        assert admin_hosts("host=127.0.0.1 port=5434 dbname=d") == ["127.0.0.1"]

    def test_comma_separated_hosts(self) -> None:
        """libpq mengizinkan daftar host; semuanya harus diperiksa."""
        assert admin_hosts("host=127.0.0.1,db.internal dbname=d") == ["127.0.0.1", "db.internal"]

    def test_no_host_means_local_socket_or_default(self) -> None:
        assert admin_hosts("dbname=payment_guard") == []


class TestRequireLoopbackAdmin:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_is_accepted(self, host: str) -> None:
        require_loopback_admin(admin_url(host))

    def test_url_without_host_is_accepted(self) -> None:
        """Tanpa host berarti socket lokal atau default localhost."""
        require_loopback_admin("dbname=payment_guard user=admin")

    @pytest.mark.parametrize("host", ["10.0.0.5", "db.internal", "0.0.0.0", "203.0.113.9"])
    def test_non_loopback_is_rejected(self, host: str) -> None:
        with pytest.raises(MigrationError) as exc:
            require_loopback_admin(admin_url(host))
        message = str(exc.value)
        assert "bukan loopback" in message
        assert host in message, "pesan harus menyebut host yang ditolak"

    def test_one_remote_host_in_a_list_is_enough_to_reject(self) -> None:
        with pytest.raises(MigrationError, match=r"db\.internal"):
            require_loopback_admin("host=127.0.0.1,db.internal dbname=d")

    def test_message_names_the_escape_hatch(self) -> None:
        with pytest.raises(MigrationError) as exc:
            require_loopback_admin(admin_url("db.internal"))
        assert "--allow-remote-admin" in str(exc.value)

    def test_message_does_not_contain_the_url_or_password(self) -> None:
        with pytest.raises(MigrationError) as exc:
            require_loopback_admin(admin_url("db.internal"))
        message = str(exc.value)
        assert ADMIN_PASSWORD not in message
        assert "postgresql://" not in message
        assert "admin:" not in message

    @pytest.mark.parametrize("host", ["10.0.0.5", "db.internal"])
    def test_allow_remote_opens_the_gate(self, host: str) -> None:
        require_loopback_admin(admin_url(host), allow_remote=True)


class TestBootstrapRefusesBeforeConnecting:
    """Penolakan terjadi sebelum koneksi, jadi tidak perlu database untuk tes ini."""

    def test_remote_admin_url_is_refused(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        migrator_file = tmp_path / ".env.migrator"
        with pytest.raises(MigrationError, match="bukan loopback"):
            run_bootstrap_roles(admin_url("db.internal"), env_file, migrator_file)
        # Tidak ada file yang ditulis saat ditolak.
        assert not env_file.exists()
        assert not migrator_file.exists()

    def test_refusal_message_has_no_password(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError) as exc:
            run_bootstrap_roles(
                admin_url("db.internal"), tmp_path / ".env", tmp_path / ".env.migrator"
            )
        assert ADMIN_PASSWORD not in str(exc.value)


class TestReadAdminUrl:
    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ADMIN_URL_ENV_VAR, admin_url("127.0.0.1"))
        assert read_admin_url(allow_prompt=False) == admin_url("127.0.0.1")

    def test_no_prompt_fails_without_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ADMIN_URL_ENV_VAR, raising=False)
        with pytest.raises(MigrationError) as exc:
            read_admin_url(allow_prompt=False)
        assert ADMIN_URL_ENV_VAR in str(exc.value)

    def test_prompt_uses_getpass_so_input_is_not_echoed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dites lewat jalur yang dipakai: ``getpass.getpass``, bukan ``input``.

        ``input()`` akan menampilkan URL di terminal dan meninggalkannya di
        scrollback. Patch di sini membuktikan fungsi mana yang dipanggil.
        """
        monkeypatch.delenv(ADMIN_URL_ENV_VAR, raising=False)
        calls: list[str] = []

        def fake_getpass(prompt: str = "") -> str:
            calls.append(prompt)
            return admin_url("127.0.0.1")

        def fail_input(prompt: str = "") -> str:  # pragma: no cover - harus tidak terpanggil
            raise AssertionError("input() akan meng-echo URL admin; harus pakai getpass")

        monkeypatch.setattr("payment_guard.bootstrap.getpass.getpass", fake_getpass)
        monkeypatch.setattr("builtins.input", fail_input)

        assert read_admin_url() == admin_url("127.0.0.1")
        assert len(calls) == 1
        assert ADMIN_URL_ENV_VAR in calls[0]

    def test_empty_prompt_answer_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ADMIN_URL_ENV_VAR, raising=False)
        monkeypatch.setattr("payment_guard.bootstrap.getpass.getpass", lambda *_: "   ")
        with pytest.raises(MigrationError, match="kosong"):
            read_admin_url()

    def test_prompt_text_contains_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ADMIN_URL_ENV_VAR, raising=False)
        prompts: list[str] = []

        def capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return admin_url("127.0.0.1")

        monkeypatch.setattr("payment_guard.bootstrap.getpass.getpass", capture)
        read_admin_url()
        assert "postgresql://" not in prompts[0]


class TestNoCodePathPrintsTheAdminUrl:
    """Tidak ada ``print`` di modul bootstrap, dan CLI hanya mencetak ringkasan."""

    def test_bootstrap_module_has_no_print_calls(self) -> None:
        import ast
        from pathlib import Path as P

        source = (
            P(__file__).resolve().parents[2] / "src" / "payment_guard" / "bootstrap.py"
        ).read_text(encoding="utf-8")
        prints = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        assert prints == [], f"bootstrap.py mencetak di baris {prints}"

    def test_cli_deletes_the_admin_url_after_use(self) -> None:
        """Referensi dibuang begitu selesai, supaya tidak ikut traceback lokal."""
        import ast
        from pathlib import Path as P

        source = (P(__file__).resolve().parents[2] / "src" / "payment_guard" / "cli.py").read_text(
            encoding="utf-8"
        )
        deleted = [
            target.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Delete)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert "admin_url" in deleted
