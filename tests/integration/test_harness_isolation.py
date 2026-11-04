"""Penjaga harness itu sendiri: ia tidak boleh bisa menyentuh cluster operator.

Tes di berkas ini **tidak** meminta fixture ``admin_url``, jadi ia berjalan
tanpa cluster tes sama sekali. Itu disengaja: pemeriksaan yang melindungi
cluster operator harus bisa dijalankan bahkan ketika tidak ada cluster tes yang
hidup.

Latar belakangnya satu insiden nyata. Harness versi sebelumnya punya URL
**bawaan** yang menunjuk tepat ke ``127.0.0.1:5434/payment_guard``, yaitu
database operator, dan fixture-nya menghapus role ``*_app`` beserta
password-nya. Role Postgres bersifat cluster-wide, jadi satu run tes cukup
untuk membuat ``.env`` operator berhenti cocok. Setiap tes di bawah menjaga
satu bagian dari jalan yang membuat itu terjadi.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import conftest as harness
from conftest import (
    OPERATOR_PORT,
    TEST_ADMIN_URL_ENV,
    TEST_DB_SUFFIX,
    TEST_ENV_FILENAME,
    TEST_ROLE_REGEX,
    RoleNames,
    isolation_failure,
    read_test_admin_url,
    require_isolated_url,
)
from payment_guard.privileges import (
    GROUP_ROLES,
    LOGIN_ROLES,
    OWNER_ROLE,
    PROCESSES,
    RolePrefixError,
    require_role_prefix,
)

#: URL yang merusak ``.env`` operator pada insiden itu. Ia muncul di berkas ini
#: **hanya** sebagai masukan yang harus ditolak.
OPERATOR_URL = "postgresql://guard_admin:devonly_not_a_secret@127.0.0.1:5434/payment_guard"

#: URL cluster tes yang sah, untuk pembanding.
GOOD_URL = "postgresql://guard_admin:devonly_not_a_secret@127.0.0.1:5435/payment_guard_test"


class TestTheIncidentCannotRepeat:
    def test_the_operator_url_is_refused(self) -> None:
        assert isolation_failure(OPERATOR_URL) is not None

    def test_it_is_refused_even_when_the_environment_variable_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Menyetel variabel lingkungan tidak cukup untuk membuka jalan.

        Inilah bentuk insiden yang sebenarnya: harness mengambil URL dari
        tempat yang dipercaya, lalu memakainya tanpa bertanya apakah itu
        cluster tes.
        """
        monkeypatch.setenv(TEST_ADMIN_URL_ENV, OPERATOR_URL)
        assert read_test_admin_url() == OPERATOR_URL
        assert isolation_failure(read_test_admin_url()) is not None

    def test_the_refusal_names_the_operator_port(self) -> None:
        alasan = isolation_failure(OPERATOR_URL)
        assert alasan is not None
        assert str(OPERATOR_PORT) in alasan

    def test_the_operator_port_is_refused_even_with_a_test_database_name(self) -> None:
        """Dua penjaga, bukan satu.

        Nama database yang berakhiran ``_test`` tidak membuat cluster operator
        menjadi cluster tes: role-nya tetap cluster-wide dan tetap milik
        operator.
        """
        url = f"postgresql://guard_admin:x@127.0.0.1:{OPERATOR_PORT}/apa_saja_test"
        alasan = isolation_failure(url)
        assert alasan is not None
        assert str(OPERATOR_PORT) in alasan

    def test_there_is_no_default_url_in_the_harness(self) -> None:
        """Tidak ada URL bawaan, dan tidak ada yang menyerupainya.

        Diperiksa atas teks sumber: sebuah konstanta bawaan adalah satu baris,
        dan satu baris itulah yang menyebabkan insiden.
        """
        source = Path(harness.__file__).read_text(encoding="utf-8")
        assert "DEFAULT_TEST_ADMIN_URL" not in source
        assert "payment_guard" not in source.replace("payment_guard.", "").replace(
            "payment_guard_test", ""
        ), "harness menyebut nama database operator"

    def test_an_empty_url_is_refused_rather_than_guessed(self) -> None:
        alasan = isolation_failure("")
        assert alasan is not None
        assert TEST_ADMIN_URL_ENV in alasan


class TestUrlShapeGuard:
    def test_a_proper_test_url_passes(self) -> None:
        assert isolation_failure(GOOD_URL) is None

    @pytest.mark.parametrize(
        "dbname", ["payment_guard", "postgres", "testing", "test_payment", "pgtest_abc"]
    )
    def test_a_database_name_not_ending_in_test_is_refused(self, dbname: str) -> None:
        url = f"postgresql://guard_admin:x@127.0.0.1:5435/{dbname}"
        alasan = isolation_failure(url)
        assert alasan is not None
        assert TEST_DB_SUFFIX in alasan

    @pytest.mark.parametrize("host", ["10.1.2.3", "db.example.com", "192.168.1.5", "0.0.0.0"])
    def test_a_non_loopback_host_is_refused(self, host: str) -> None:
        url = f"postgresql://guard_admin:x@{host}:5435/payment_guard_test"
        alasan = isolation_failure(url)
        assert alasan is not None
        assert "loopback" in alasan

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
    def test_every_loopback_spelling_is_accepted(self, host: str) -> None:
        """IPv6 di URL ditulis dalam tanda kurung; libpq mem-parse-nya ke ``::1``.

        Karena itu :data:`LOOPBACK_HOSTS` memuat kedua ejaan. Tanpa tanda
        kurung, ``postgresql://user@::1:5435/db`` ter-parse dengan host kosong
        dan ditolak — yang benar, tapi bukan karena alasan yang disangka.
        """
        url = f"postgresql://guard_admin:x@{host}:5435/payment_guard_test"
        assert isolation_failure(url) is None

    def test_a_bare_ipv6_address_without_brackets_is_refused(self) -> None:
        """Ditolak, walau bukan oleh penjaga host.

        Tanpa tanda kurung, libpq membaca ``::1:5435`` sebagai port, jadi yang
        menyalakan penolakan adalah "port bukan bilangan". Hasilnya benar dan
        itu yang penting; tes ini mencatat jalurnya supaya pesan yang muncul
        tidak terasa seperti bug ketika seseorang menemuinya.
        """
        url = "postgresql://guard_admin:x@::1:5435/payment_guard_test"
        alasan = isolation_failure(url)
        assert alasan is not None
        assert "port" in alasan

    def test_a_missing_port_is_refused(self) -> None:
        """Port wajib eksplisit: 'bukan 5434' tidak boleh bergantung pada default."""
        url = "postgresql://guard_admin:x@127.0.0.1/payment_guard_test"
        alasan = isolation_failure(url)
        assert alasan is not None
        assert "port" in alasan

    def test_a_non_numeric_port_is_refused(self) -> None:
        url = "postgresql://guard_admin:x@127.0.0.1:bukan-angka/payment_guard_test"
        assert isolation_failure(url) is not None

    def test_an_unparseable_url_is_refused(self) -> None:
        assert isolation_failure("ini bukan url sama sekali") is not None

    @pytest.mark.parametrize("port", [5432, 5433, 5435, 15432])
    def test_other_loopback_ports_are_allowed(self, port: int) -> None:
        """Hanya port operator yang dilarang, bukan semua port selain 5435."""
        url = f"postgresql://guard_admin:x@127.0.0.1:{port}/payment_guard_test"
        assert isolation_failure(url) is None


class TestRequireIsolatedUrlStopsTheSession:
    def test_a_bad_url_ends_the_whole_session(self) -> None:
        """``pytest.exit``, bukan ``fail`` atau ``skip``.

        Satu tes yang gagal bisa diabaikan; sesi yang tidak boleh berjalan
        tidak boleh bisa diabaikan.
        """
        with pytest.raises(pytest.exit.Exception) as info:
            require_isolated_url(OPERATOR_URL)
        assert str(OPERATOR_PORT) in str(info.value)

    def test_the_message_tells_the_operator_what_to_run(self) -> None:
        with pytest.raises(pytest.exit.Exception) as info:
            require_isolated_url(OPERATOR_URL)
        pesan = str(info.value)
        assert "--profile test" in pesan
        assert TEST_ADMIN_URL_ENV in pesan

    def test_a_good_url_passes_through_unchanged(self) -> None:
        assert require_isolated_url(GOOD_URL) == GOOD_URL


class TestTheHarnessNeverReadsOperatorEnvFiles:
    def test_only_the_test_env_file_is_consulted(self) -> None:
        """Diperiksa atas sumber fungsinya, bukan seluruh modul.

        Modul ini memang menyebut ``.env`` dan ``.env.migrator``: ia membuat
        keduanya di ``tmp_path`` untuk bootstrap dalam tes. Yang tidak boleh
        adalah **membacanya** sebagai sumber URL admin.
        """
        source = inspect.getsource(read_test_admin_url)
        assert TEST_ENV_FILENAME in source
        assert "RUNTIME_ENV_FILENAME" not in source
        assert "MIGRATOR_ENV_FILENAME" not in source

    def test_operator_env_files_are_ignored_even_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hermetis: akar repo tiruan yang memuat .env dan .env.migrator."""
        (tmp_path / ".env").write_text(f"GUARD_DB_URL={OPERATOR_URL}\n", encoding="utf-8")
        (tmp_path / ".env.migrator").write_text(
            f"MIGRATOR_DB_URL={OPERATOR_URL}\n", encoding="utf-8"
        )
        monkeypatch.delenv(TEST_ADMIN_URL_ENV, raising=False)
        monkeypatch.setattr(harness, "_repo_root", lambda: tmp_path)

        assert read_test_admin_url() == ""

    def test_the_test_env_file_is_read_when_it_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / TEST_ENV_FILENAME).write_text(
            f"{TEST_ADMIN_URL_ENV}={GOOD_URL}\n", encoding="utf-8"
        )
        monkeypatch.delenv(TEST_ADMIN_URL_ENV, raising=False)
        monkeypatch.setattr(harness, "_repo_root", lambda: tmp_path)

        assert read_test_admin_url() == GOOD_URL

    def test_the_environment_variable_wins_over_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / TEST_ENV_FILENAME).write_text(
            f"{TEST_ADMIN_URL_ENV}={GOOD_URL}\n", encoding="utf-8"
        )
        lain = GOOD_URL.replace("5435", "5436")
        monkeypatch.setenv(TEST_ADMIN_URL_ENV, lain)
        monkeypatch.setattr(harness, "_repo_root", lambda: tmp_path)

        assert read_test_admin_url() == lain


class TestRolePrefixes:
    def test_the_prefix_shape_is_what_teardown_looks_for(self) -> None:
        """Pola di :data:`TEST_ROLE_REGEX` harus cocok dengan yang dibangkitkan.

        Kalau keduanya menyimpang, role sisa run yang mati tidak akan pernah
        dibersihkan, dan tidak ada yang akan memberi tahu.
        """
        import re

        names = RoleNames(prefix="ta1b2c3d4_")
        pola = re.compile(TEST_ROLE_REGEX)
        for name in names.all_prefixed():
            assert pola.match(name), name

    def test_the_fixture_prefix_matches_the_pattern(self, role_prefix: str) -> None:
        import re

        assert re.match(TEST_ROLE_REGEX, f"{role_prefix}guard_app")

    def test_login_and_owner_roles_are_prefixed(self, roles: RoleNames) -> None:
        for process in PROCESSES:
            assert roles.login(process) != LOGIN_ROLES[process]
            assert roles.login(process).endswith(LOGIN_ROLES[process])
        assert roles.owner != OWNER_ROLE
        assert roles.owner.endswith(OWNER_ROLE)

    def test_group_roles_are_not_prefixed_and_that_is_documented(self, roles: RoleNames) -> None:
        """``0002_roles.sql`` menuliskan nama role grup harfiah, dan ia terkunci.

        Batas ini kecil dan jujur: role grup NOLOGIN, tanpa password, dan tidak
        muncul di file env mana pun. Yang hilang pada insiden itu adalah role
        **login** beserta password-nya.
        """
        for process in PROCESSES:
            assert roles.group(process) == GROUP_ROLES[process]

    def test_prefixed_roles_never_include_a_group_role(self, roles: RoleNames) -> None:
        """Teardown memakai daftar ini; ia tidak boleh menghapus nama bersama."""
        assert not set(roles.all_prefixed()) & set(GROUP_ROLES.values())

    def test_two_runs_get_different_prefixes(self) -> None:
        from conftest import role_prefix as fixture

        satu = fixture.__wrapped__()  # type: ignore[attr-defined]
        dua = fixture.__wrapped__()  # type: ignore[attr-defined]
        assert satu != dua

    def test_an_empty_prefix_yields_no_prefixed_roles(self) -> None:
        assert RoleNames(prefix="").all_prefixed() == ()

    @pytest.mark.parametrize(
        "buruk", ["T1234_", "1abc_", "tabc", "ta-bc_", "t_" * 20, "ta bc_", "ta.bc_"]
    )
    def test_a_malformed_prefix_is_refused(self, buruk: str) -> None:
        with pytest.raises(RolePrefixError):
            require_role_prefix(buruk)

    def test_the_empty_prefix_is_what_production_uses(self) -> None:
        assert require_role_prefix("") == ""
