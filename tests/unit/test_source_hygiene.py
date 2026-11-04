"""Aturan keras yang berlaku untuk seluruh ``src/``.

1. Tidak ada ``float`` (uang hanya bilangan bulat base units).
2. Tidak ada SQL yang dirakit lewat string.

Setiap pemeriksa di sini diuji dua kali: terhadap ``src/`` yang sesungguhnya,
dan terhadap contoh pelanggaran yang ditanam. Yang kedua mencegah tes ini
berubah menjadi tes kosong yang selalu lulus.

Security v1.1 E6 / ST-24. Arsitektur v1.1 bagian 12.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

#: Pola SQL yang kuat. Huruf besar saja: pesan error dan docstring proyek ini
#: berbahasa Indonesia huruf kecil, jadi tidak ada tabrakan dengan prosa.
SQL_PATTERN = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE\s|DELETE\s+FROM|GRANT\s|REVOKE\s|"
    r"CREATE\s+(TABLE|ROLE|INDEX|FUNCTION|TRIGGER|SCHEMA|SEQUENCE)|"
    r"DROP\s|ALTER\s|TRUNCATE\s)"
)

#: Nama method yang dipakai untuk merakit string.
_ASSEMBLY_METHODS = frozenset({"format", "join"})


def source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _looks_like_sql(text: str) -> bool:
    return bool(SQL_PATTERN.search(text))


def _literal_parts(node: ast.JoinedStr) -> str:
    return "".join(
        v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )


# --------------------------------------------------------------------------- #
# Pemeriksa 1: tidak ada float
# --------------------------------------------------------------------------- #


def _isinstance_guard_names(tree: ast.AST) -> set[int]:
    """Identitas node ``ast.Name`` yang menjadi argumen ``isinstance(...)``.

    Menyebut ``float`` di dalam ``isinstance`` adalah **penolakan** float,
    kebalikan dari memakainya. ``canonical.py`` dan ``money.py`` harus bisa
    menolak float, dan untuk itu mereka harus menyebut nama tipenya.

    Hanya pola itu yang dikecualikan. ``float(x)``, ``1.5``, dan anotasi
    ``: float`` tetap ditandai; ada tes yang membuktikannya.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        for argument in node.args[1:]:
            # isinstance(x, float) dan isinstance(x, int | float)
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Name):
                    allowed.add(id(inner))
    return allowed


def find_float_usage(tree: ast.AST) -> list[tuple[int, str]]:
    """Cari literal float dan pemakaian nama ``float``.

    Pengecualian tunggal: ``float`` sebagai argumen ``isinstance`` (lihat
    :func:`_isinstance_guard_names`).
    """
    findings: list[tuple[int, str]] = []
    guards = _isinstance_guard_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            findings.append((node.lineno, f"literal float {node.value!r}"))
        elif isinstance(node, ast.Name) and node.id == "float":
            if id(node) not in guards:
                findings.append((node.lineno, "nama 'float'"))
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            findings.append((node.lineno, "atribut '.float'"))
    return findings


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_float_in_src(path: Path) -> None:
    findings = find_float_usage(ast.parse(path.read_text(encoding="utf-8")))
    assert not findings, f"{path.name}: " + "; ".join(
        f"baris {ln}: {what}" for ln, what in findings
    )


@pytest.mark.parametrize(
    "code",
    [
        "x = 1.5",
        "x = float('1')",
        "def f(a: float) -> None: ...",
        "x = 1e10",
        "TOTAL = 0.1 + 0.2",
    ],
)
def test_float_checker_catches_planted_violations(code: str) -> None:
    assert find_float_usage(ast.parse(code)), code


@pytest.mark.parametrize("code", ["x = 1", "x = 10 ** 6", "x = int('3')", "x = 2**63 - 1"])
def test_float_checker_allows_integers(code: str) -> None:
    assert find_float_usage(ast.parse(code)) == []


@pytest.mark.parametrize(
    "code",
    [
        # Menolak float adalah kebalikan dari memakainya.
        "if isinstance(x, float): raise ValueError",
        "if isinstance(x, int | float): raise ValueError",
        "assert not isinstance(x, float)",
    ],
)
def test_float_checker_allows_isinstance_guards(code: str) -> None:
    assert find_float_usage(ast.parse(code)) == []


@pytest.mark.parametrize(
    "code",
    [
        # Pengecualian isinstance tidak boleh membuka lubang.
        "x = float('1')",
        "x = 1.5",
        "def f(a: float) -> None: ...",
        "x = isinstance(y, int) and float(z)",
        "x = numpy.float",
    ],
)
def test_isinstance_exception_does_not_open_a_hole(code: str) -> None:
    assert find_float_usage(ast.parse(code)), code


# --------------------------------------------------------------------------- #
# Pemeriksa 2: tidak ada SQL yang dirakit lewat string
# --------------------------------------------------------------------------- #


def find_sql_assembly(tree: ast.AST) -> list[tuple[int, str]]:
    """Cari SQL yang dirakit lewat f-string, ``+``, ``%``, ``.format``, atau ``.join``.

    Satu-satunya cara yang diizinkan untuk SQL dinamis adalah composable
    ``psycopg.sql`` (``sql.SQL(...).format(sql.Identifier(...))``): di sana
    driver yang melakukan quoting, dan penerima ``.format`` adalah objek
    ``sql.SQL``, bukan ``str``. Karena itu pemeriksa ini hanya menandai
    ``.format``/``.join`` yang penerimanya literal ``str``.
    """
    findings: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # f-string yang bagian literalnya menyerupai SQL
        if isinstance(node, ast.JoinedStr):
            if _looks_like_sql(_literal_parts(node)):
                findings.append((node.lineno, "SQL di dalam f-string"))

        # "SQL ..." + x   atau   "SQL ..." % x
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    if _looks_like_sql(side.value):
                        op = "+" if isinstance(node.op, ast.Add) else "%"
                        findings.append((node.lineno, f"SQL dirakit dengan '{op}'"))
                        break

        elif isinstance(node, ast.Call):
            func = node.func
            # "SQL ...".format(...) atau "...".join([...]) pada literal str
            if isinstance(func, ast.Attribute) and func.attr in _ASSEMBLY_METHODS:
                receiver = func.value
                if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
                    if _looks_like_sql(receiver.value):
                        findings.append((node.lineno, f"SQL dirakit dengan .{func.attr}()"))
            # execute(f"...") atau execute("..." + x): apa pun isinya, tidak boleh
            if isinstance(func, ast.Attribute) and func.attr in {"execute", "executemany"}:
                if node.args and isinstance(node.args[0], ast.JoinedStr | ast.BinOp):
                    findings.append((node.lineno, f"{func.attr}() menerima string yang dirakit"))

    return findings


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_sql_string_assembly_in_src(path: Path) -> None:
    findings = find_sql_assembly(ast.parse(path.read_text(encoding="utf-8")))
    assert not findings, f"{path.name}: " + "; ".join(
        f"baris {ln}: {what}" for ln, what in findings
    )


@pytest.mark.parametrize(
    "code",
    [
        'q = f"SELECT * FROM {table}"',
        'q = "SELECT * FROM " + table',
        'q = "SELECT * FROM %s" % table',
        'q = "GRANT SELECT ON {} TO x".format(table)',
        'q = "SELECT a FROM t WHERE id IN ({})".format(",".join(ids))',
        'cur.execute(f"DELETE FROM t WHERE id = {bad}")',
        'cur.execute("UPDATE t SET a = 1 WHERE id = " + bad)',
        'q = f"ALTER TABLE {t} ADD COLUMN c int"',
    ],
)
def test_sql_checker_catches_planted_violations(code: str) -> None:
    assert find_sql_assembly(ast.parse(code)), code


@pytest.mark.parametrize(
    "code",
    [
        # Kueri berparameter: satu-satunya cara untuk nilai.
        'cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))',
        # Composable psycopg: satu-satunya cara untuk identifier.
        'cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(a, b))',
        'cur.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(name)))',
        # Rakitan string yang bukan SQL: pesan untuk operator.
        'msg = f"migrasi {v} gagal"',
        'msg = "nomor ganda: " + ", ".join(names)',
    ],
)
def test_sql_checker_allows_sanctioned_patterns(code: str) -> None:
    assert find_sql_assembly(ast.parse(code)) == [], code


# --------------------------------------------------------------------------- #
# Pemeriksa 3: SQL dinamis hanya di satu modul
# --------------------------------------------------------------------------- #


def _modules_importing(top_level: str) -> list[str]:
    """Berkas di ``src/`` yang mengimpor paket ``top_level``."""
    users = []
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == top_level or name.startswith(f"{top_level}.") for name in names):
                users.append(path.name)
                break
    return sorted(users)


def test_keccak_backend_is_confined_to_hashing() -> None:
    """``pycryptodome`` hanya boleh disentuh satu modul.

    Keccak-256 dan SHA3-256 mudah tertukar, dan backend-nya bukan stdlib.
    Memusatkannya di satu berkas membuat penggantian backend tidak menyebar
    dan membuat tinjauan manual punya satu tempat untuk dibaca.
    """
    # pycryptodome dipasang sebagai paket bernama `Crypto`.
    assert _modules_importing("Crypto") == ["hashing.py"]


def test_hashlib_sha3_is_not_used_as_a_keccak_substitute() -> None:
    """SHA3-256 dari stdlib bukan Keccak-256; jangan sampai dipakai menggantikan."""
    offenders = []
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("sha3_"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


def test_psycopg_sql_composable_is_confined_to_bootstrap() -> None:
    """``psycopg.sql`` hanya boleh dipakai bootstrap.

    Semua SQL lain statik atau berparameter. Membatasi tempat SQL dinamis
    boleh muncul membuat tinjauan manual (Security v1.1 E6) layak dikerjakan.
    """
    users = []
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports_sql = (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith("psycopg")
                and any(alias.name == "sql" for alias in node.names)
            )
            uses_attribute = (
                isinstance(node, ast.Attribute)
                and node.attr == "sql"
                and isinstance(node.value, ast.Name)
                and node.value.id == "psycopg"
            )
            if imports_sql or uses_attribute:
                users.append(path.name)
                break
    assert sorted(users) == ["bootstrap.py"], users


# --------------------------------------------------------------------------- #
# Pemeriksa 4: modul murni M1
# --------------------------------------------------------------------------- #

#: Modul yang WAJIB murni: tanpa database, jaringan, atau jam.
#: Arsitektur v1.1 bagian 12: "rules.py, money.py, addresses.py, mandate.py
#: tidak mengimpor db, jaringan, atau jam."
PURE_MODULES = (
    "money.py",
    "addresses.py",
    "canonical.py",
    "chains.py",
    "mandate.py",
    "rules.py",
    "reasons.py",
    "signing.py",
    # M4: penyajian teks tidak tepercaya. Murni dengan sengaja — ia dipanggil
    # dari CLI, tapi tidak boleh punya alasan apa pun menyentuh dunia luar.
    "untrusted.py",
)

#: Paket pihak ketiga dan stdlib yang membawa dunia luar masuk.
FORBIDDEN_IMPORTS = frozenset(
    {
        # database
        "psycopg",
        "sqlite3",
        "sqlalchemy",
        # jaringan
        "socket",
        "requests",
        "http",
        "urllib",
        "ssl",
        "asyncio",
        # jam dan sumber non-determinisme
        "time",
        "datetime",
        "random",
        "secrets",
        "uuid",
        # proses dan berkas
        "os",
        "sys",
        "subprocess",
        "pathlib",
        "io",
        "shutil",
        "tempfile",
        "logging",
    }
)

#: Modul internal yang boleh diimpor modul murni. ``db`` dan ``config`` tidak
#: ada di sini dengan sengaja.
ALLOWED_INTERNAL = frozenset(
    {
        "money",
        "addresses",
        "canonical",
        "chains",
        "mandate",
        "rules",
        "reasons",
        "hashing",
        "signing",
    }
)

#: Pemanggilan jam yang tidak boleh ada, walau modulnya entah bagaimana masuk.
FORBIDDEN_CLOCK_ATTRIBUTES = frozenset(
    {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "time_ns"}
)


def _module_path(name: str) -> Path:
    return SRC / "payment_guard" / name


def _imports_of(tree: ast.AST) -> set[str]:
    """Nama paket tingkat atas yang diimpor."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _internal_imports_of(tree: ast.AST) -> set[str]:
    """Submodul ``payment_guard`` yang diimpor."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "payment_guard" and len(parts) > 1:
                names.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "payment_guard" and len(parts) > 1:
                    names.add(parts[1])
    return names


@pytest.mark.parametrize("name", PURE_MODULES)
def test_pure_module_imports_nothing_impure(name: str) -> None:
    tree = ast.parse(_module_path(name).read_text(encoding="utf-8"))
    offending = _imports_of(tree) & FORBIDDEN_IMPORTS
    assert not offending, f"{name} mengimpor {sorted(offending)}"


@pytest.mark.parametrize("name", PURE_MODULES)
def test_pure_module_does_not_import_db_or_config(name: str) -> None:
    """Modul murni tidak boleh tahu soal koneksi atau file env."""
    tree = ast.parse(_module_path(name).read_text(encoding="utf-8"))
    internal = _internal_imports_of(tree)
    offending = internal - ALLOWED_INTERNAL
    assert not offending, f"{name} mengimpor payment_guard.{sorted(offending)}"
    assert "db" not in internal
    assert "config" not in internal


@pytest.mark.parametrize("name", PURE_MODULES)
def test_pure_module_never_reads_a_clock(name: str) -> None:
    """Waktu selalu disuntikkan sebagai parameter, tidak pernah dibaca sendiri."""
    tree = ast.parse(_module_path(name).read_text(encoding="utf-8"))
    offending = [
        f"baris {node.lineno}: .{node.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_CLOCK_ATTRIBUTES
    ]
    assert not offending, f"{name}: {offending}"


def test_the_purity_checkers_would_catch_a_regression() -> None:
    """Penjaga: pemeriksa di atas bukan tes kosong."""
    planted = ast.parse(
        "import time\n"
        "import psycopg\n"
        "from payment_guard.db import connect\n"
        "from datetime import datetime\n"
        "x = datetime.now()\n"
    )
    assert _imports_of(planted) & FORBIDDEN_IMPORTS == {"time", "psycopg", "datetime"}
    assert _internal_imports_of(planted) == {"db"}
    clock_calls = [
        node
        for node in ast.walk(planted)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_CLOCK_ATTRIBUTES
    ]
    assert len(clock_calls) == 1


def test_m1_modules_all_exist() -> None:
    """Kalau satu modul hilang, tes murni di atas akan lolos tanpa memeriksa apa pun."""
    for name in PURE_MODULES:
        assert _module_path(name).is_file(), name


# --------------------------------------------------------------------------- #
# Pemeriksa 5: modul M3 boleh menyentuh database, tapi tidak boleh melanggar M1
# --------------------------------------------------------------------------- #

#: Modul M3 yang memang membuka transaksi dan berkas. Mereka BUKAN murni, tapi
#: tetap terikat aturan lain: tanpa float, tanpa SQL dirakit string, dan tanpa
#: membuat modul murni jadi tidak murni.
IMPURE_M3_MODULES = (
    "db.py",
    "config.py",
    "events.py",
    "guard_service.py",
    "decision_service.py",
    "mcp_server.py",
    # M4
    "approvals_cli.py",
    "demo_agent.py",
    # M5
    "chain.py",
    "executor.py",
)


@pytest.mark.parametrize("name", IMPURE_M3_MODULES)
def test_m3_module_exists(name: str) -> None:
    """Kalau modulnya hilang, tes di bawah lulus tanpa memeriksa apa pun."""
    assert (SRC / "payment_guard" / name).is_file(), name


@pytest.mark.parametrize("name", IMPURE_M3_MODULES)
def test_m3_module_has_no_float(name: str) -> None:
    tree = ast.parse((SRC / "payment_guard" / name).read_text(encoding="utf-8"))
    findings = find_float_usage(tree)
    assert not findings, f"{name}: " + "; ".join(f"baris {ln}: {w}" for ln, w in findings)


@pytest.mark.parametrize("name", IMPURE_M3_MODULES)
def test_m3_module_assembles_no_sql(name: str) -> None:
    tree = ast.parse((SRC / "payment_guard" / name).read_text(encoding="utf-8"))
    findings = find_sql_assembly(tree)
    assert not findings, f"{name}: " + "; ".join(f"baris {ln}: {w}" for ln, w in findings)


def test_pure_modules_do_not_import_the_m3_modules() -> None:
    """Arah ketergantungan hanya satu: M3 memakai M1, bukan sebaliknya.

    Kalau ``rules.py`` sampai mengimpor ``decision_service``, aturan berhenti
    jadi fungsi murni dan tes paralel kehilangan artinya.
    """
    impure_names = {name.removesuffix(".py") for name in IMPURE_M3_MODULES}
    offenders = []
    for name in PURE_MODULES:
        tree = ast.parse((SRC / "payment_guard" / name).read_text(encoding="utf-8"))
        leaked = _internal_imports_of(tree) & impure_names
        if leaked:
            offenders.append(f"{name} -> {sorted(leaked)}")
    assert offenders == [], offenders


#: Modul yang boleh menulis ``decisions``, dan alasannya.
#:
#: ``decision_service`` menulis baris baru dari ``check_payment``.
#: ``approvals_cli`` (M4) memperbarui baris ``needs_human`` menjadi disetujui,
#: ditolak, atau kedaluwarsa — itu hak role approver di ``0002_roles.sql``,
#: bukan hak Guard. Keduanya wajib memegang lock ``agent:``; tes di bawah
#: memeriksanya satu per satu.
DECISION_WRITERS = ("decision_service.py", "approvals_cli.py")


def test_only_decision_service_inserts_decisions() -> None:
    """Baris ``decisions`` baru hanya lahir di satu tempat."""
    offenders = []
    for path in sorted((SRC / "payment_guard").rglob("*.py")):
        if path.name == "decision_service.py":
            continue
        if "INSERT INTO decisions" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], offenders


def test_only_known_modules_update_decisions() -> None:
    """Daftar penulis ``decisions`` tertutup, supaya lock-nya tidak bisa dilewati."""
    offenders = []
    for path in sorted((SRC / "payment_guard").rglob("*.py")):
        if path.name in DECISION_WRITERS:
            continue
        if "UPDATE decisions" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], offenders


@pytest.mark.parametrize("name", DECISION_WRITERS)
def test_decision_writers_take_the_agent_lock(name: str) -> None:
    """Penjaga bentuk: setiap penulis ``decisions`` mengambil lock ``agent:``."""
    source = (SRC / "payment_guard" / name).read_text(encoding="utf-8")
    assert "take_xact_lock" in source, name
    assert '"agent"' in source, name


#: Modul Executor. Dipisah dari daftar di atas karena ada aturan tambahan
#: yang hanya berlaku untuk keduanya: mereka tidak boleh punya jalur apa pun
#: ke kunci penanda tangan Guard.
EXECUTOR_MODULES = ("chain.py", "executor.py")

#: Nama yang menandakan akses ke bahan kunci penanda tangan Guard.
SIGNING_KEY_NAMES = (
    "GUARD_SIGNING_KEY_PATH",
    "load_signing_identity",
    "SigningIdentity",
    "signing_key_path",
    "Ed25519PrivateKey",
)


@pytest.mark.parametrize("name", EXECUTOR_MODULES)
def test_executor_module_exists(name: str) -> None:
    """Kalau modulnya hilang, tes di bawah lulus tanpa memeriksa apa pun."""
    assert (SRC / "payment_guard" / name).is_file(), name


@pytest.mark.parametrize("name", EXECUTOR_MODULES)
@pytest.mark.parametrize("forbidden", SIGNING_KEY_NAMES)
def test_executor_never_names_the_signing_key(name: str, forbidden: str) -> None:
    """Executor hanya memegang kunci publik Guard (Arsitektur v1.1 bagian 2).

    Diperiksa atas teks sumber, bukan hanya impor: sebuah nama variabel
    environment bisa muncul sebagai string tanpa impor apa pun, dan string itu
    sudah cukup untuk membaca berkasnya.
    """
    source = (SRC / "payment_guard" / name).read_text(encoding="utf-8")
    assert forbidden not in source, f"{name} menyebut {forbidden}"


@pytest.mark.parametrize("name", EXECUTOR_MODULES)
def test_executor_does_not_import_guard_or_approvals(name: str) -> None:
    """Executor tidak memuat modul proses lain.

    ``guard_service`` dan ``approvals_cli`` keduanya membuka berkas kunci
    penanda tangan. Mengimpor salah satunya akan memberi Executor jalur ke
    bahan kunci itu walau ia tidak pernah memakainya dengan sengaja.
    """
    tree = ast.parse((SRC / "payment_guard" / name).read_text(encoding="utf-8"))
    internal = _internal_imports_of(tree)
    for forbidden in ("guard_service", "approvals_cli", "keygen", "mcp_server"):
        assert forbidden not in internal, f"{name} mengimpor {forbidden}"


def test_only_the_executor_builds_transactions() -> None:
    """``encode_transfer`` hanya dipanggil dari satu modul.

    Calldata adalah satu-satunya tempat tujuan dan jumlah pembayaran
    ditetapkan. Kalau pemanggilnya menyebar, "transaksi dibangun dari payload"
    berhenti bisa diperiksa dengan membaca satu berkas.
    """
    callers = []
    for path in source_files():
        if path.name == "chain.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "encode_transfer":
                    callers.append(path.name)
                    break
    assert sorted(set(callers)) == ["executor.py"], callers


def test_web3_is_confined_to_the_chain_module() -> None:
    """``web3`` dan ``eth_account`` hanya boleh disentuh satu modul.

    Alasannya sama dengan ``pycryptodome`` di ``hashing.py``: satu tempat untuk
    dibaca saat backend-nya diganti, dan satu tempat yang perlu ditinjau manual
    karena di situlah uang ditandatangani.
    """
    assert _modules_importing("web3") == ["chain.py"]
    assert _modules_importing("eth_account") == ["chain.py"]


def test_no_module_reads_the_process_clock_for_decisions() -> None:
    """Jam keputusan selalu jam database (Arsitektur v1.1 prinsip 5).

    ``guard_service`` boleh memakai ``threading.Event.wait`` untuk interval
    heartbeat, yang bukan penilaian waktu; yang dilarang adalah membaca jam
    proses lalu memakainya sebagai waktu keputusan.
    """
    for name in ("decision_service.py", "events.py"):
        tree = ast.parse((SRC / "payment_guard" / name).read_text(encoding="utf-8"))
        imported = _imports_of(tree)
        assert "time" not in imported, name
        assert "datetime" not in imported, name
