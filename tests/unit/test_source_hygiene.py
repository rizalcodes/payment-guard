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
    "mandate.py",
    "rules.py",
    "reasons.py",
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
    {"money", "addresses", "canonical", "mandate", "rules", "reasons", "hashing"}
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
