"""Hasilkan ``docs/schema.md`` dari ``migrations/``.

Jalankan dari akar repo:

    uv run --no-sync python scripts/generate_schema_doc.py

Tanpa argumen ia menulis ``docs/schema.md``. Dengan ``--check`` ia hanya
membandingkan dan keluar dengan kode 1 bila berbeda — bentuk yang sama yang
dipakai ``tests/unit/test_schema_doc.py``.

Mengapa digenerate, bukan ditulis
---------------------------------

``migrations/0001_init.sql`` dan ``0002_roles.sql`` terkunci: skema hanya
berubah lewat migrasi baru. Dokumen skema yang ditulis tangan akan basi pada
migrasi berikutnya, dan dokumen skema yang basi lebih buruk daripada tidak ada
— ia dipakai orang untuk memutuskan apa yang boleh ditulis ke mana.

Parsernya sederhana dan sengaja begitu: ia hanya perlu memahami dua berkas yang
sudah tetap bentuknya. Yang menjaganya tetap benar bukan kecerdasan parser,
melainkan tes yang membandingkan hasilnya dengan berkas yang ter-commit; kalau
migrasi berubah dan parser tidak mengerti, tes gagal dan seseorang membacanya.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Jumlah tabel yang **didefinisikan di migrasi**. Penjaga supaya parser yang
#: berhenti mengerti tidak menghasilkan dokumen kosong yang lolos tanpa suara.
#:
#: Delapan, bukan sembilan. Database yang sudah dimigrasi memuat sembilan tabel,
#: tapi ``schema_migrations`` dibuat runner migrasi di
#: :mod:`payment_guard.migrate`, bukan oleh berkas SQL mana pun — itulah tabel
#: yang mencatat migrasi mana yang sudah jalan, jadi ia harus ada sebelum
#: migrasi pertama dijalankan.
EXPECTED_TABLE_COUNT = 8

CREATE_TABLE = re.compile(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", re.DOTALL)
GRANT = re.compile(r"GRANT\s+(.+?)\s+ON\s+(?:SEQUENCE\s+)?(\w+)\s+TO\s+(\w+);", re.DOTALL)
TRIGGER = re.compile(r"CREATE TRIGGER (\w+)\s+(.*?)\s+ON\s+(\w+)", re.DOTALL)


@dataclass
class Table:
    name: str
    source: str
    columns: list[tuple[str, str]] = field(default_factory=list)
    constraints: list[tuple[str, str]] = field(default_factory=list)
    uniques: list[str] = field(default_factory=list)


def split_top_level(body: str) -> list[str]:
    """Pisah isi ``CREATE TABLE`` pada koma di kedalaman tanda kurung nol."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    if "".join(current).strip():
        items.append("".join(current))
    return items


def strip_comments(text: str) -> str:
    """Buang komentar ``--`` sampai akhir baris."""
    return "\n".join(line.split("--", 1)[0].rstrip() for line in text.splitlines())


def collapse(text: str) -> str:
    """Satukan spasi putih menjadi satu spasi."""
    return " ".join(text.split())


def parse_tables(sql: str, source: str) -> list[Table]:
    tables: list[Table] = []
    for name, body in CREATE_TABLE.findall(sql):
        table = Table(name=name, source=source)
        for raw in split_top_level(strip_comments(body)):
            item = collapse(raw)
            if not item:
                continue
            upper = item.upper()
            if upper.startswith("CONSTRAINT "):
                parts = item.split(None, 2)
                table.constraints.append((parts[1], collapse(parts[2])))
            elif upper.startswith("UNIQUE"):
                table.uniques.append(item)
            elif upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "CHECK")):
                table.constraints.append(("(tanpa nama)", item))
            else:
                column, _, rest = item.partition(" ")
                table.columns.append((column, rest))
        tables.append(table)
    return tables


def parse_grants(sql: str) -> dict[str, list[tuple[str, str]]]:
    """Objek -> daftar (role, hak), terurut dan tanpa duplikat."""
    found: dict[str, set[tuple[str, str]]] = {}
    for privileges, obj, role in GRANT.findall(sql):
        found.setdefault(obj, set()).add((role, collapse(privileges)))
    return {obj: sorted(pairs) for obj, pairs in sorted(found.items())}


def parse_triggers(sql: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, when, table in TRIGGER.findall(sql):
        # Pemisah ASCII dengan sengaja: berkas ini dibandingkan byte-per-byte
        # di CI, dan tanda pisah non-ASCII hanya menambah satu hal yang bisa
        # berbeda antar platform tanpa alasan.
        found.setdefault(table, []).append(f"`{name}`: {collapse(when)}")
    return found


def render(migrations: Path) -> str:
    files = sorted(migrations.glob("*.sql"))
    if not files:
        raise SystemExit(f"tidak ada berkas migrasi di {migrations}")

    tables: list[Table] = []
    grants: dict[str, list[tuple[str, str]]] = {}
    triggers: dict[str, list[str]] = {}
    for path in files:
        sql = path.read_text(encoding="utf-8")
        tables.extend(parse_tables(sql, path.name))
        for obj, pairs in parse_grants(sql).items():
            grants.setdefault(obj, []).extend(pairs)
        for table, items in parse_triggers(sql).items():
            triggers.setdefault(table, []).extend(items)

    if len(tables) != EXPECTED_TABLE_COUNT:
        raise SystemExit(
            f"parser menemukan {len(tables)} tabel, bukan {EXPECTED_TABLE_COUNT}. "
            "Kalau migrasi memang bertambah, naikkan EXPECTED_TABLE_COUNT; kalau "
            "tidak, parsernya berhenti mengerti bentuk SQL-nya."
        )

    out: list[str] = []
    out.append("# Skema database")
    out.append("")
    out.append("**Dokumen turunan. Jangan diedit tangan.**")
    out.append("")
    out.append("Dihasilkan dari `migrations/` oleh `scripts/generate_schema_doc.py`.")
    out.append("`tests/unit/test_schema_doc.py` membandingkannya dengan migrasi, jadi")
    out.append("berkas ini tidak bisa basi tanpa tes gagal. Untuk memperbaruinya:")
    out.append("")
    out.append("```")
    out.append("uv run --no-sync python scripts/generate_schema_doc.py")
    out.append("```")
    out.append("")
    out.append("Sumber kebenaran tetap berkas SQL-nya. Keduanya terkunci: `0001_init.sql`")
    out.append("dan `0002_roles.sql` tidak diubah lagi, dan perubahan skema berjalan lewat")
    out.append("migrasi baru.")
    out.append("")
    out.append("Berkas yang dibaca: " + ", ".join(f"`{p.name}`" for p in files) + ".")
    out.append("")

    out.append("## Tabel")
    out.append("")
    for table in tables:
        out.append(f"### `{table.name}`")
        out.append("")
        out.append(f"Dibuat di `{table.source}`.")
        out.append("")
        out.append("| Kolom | Tipe dan batasan |")
        out.append("|---|---|")
        for column, rest in table.columns:
            out.append(f"| `{column}` | `{rest}` |" if rest else f"| `{column}` | |")
        out.append("")
        if table.uniques:
            out.append("Unik:")
            out.append("")
            for item in table.uniques:
                out.append(f"- `{item}`")
            out.append("")
        if table.constraints:
            out.append("CHECK bernama:")
            out.append("")
            for name, body in table.constraints:
                out.append(f"- `{name}`: `{body}`")
            out.append("")
        if table.name in triggers:
            out.append("Trigger:")
            out.append("")
            for item in triggers[table.name]:
                out.append(f"- {item}")
            out.append("")

    out.append("## Hak akses, ringkas")
    out.append("")
    out.append("Hak diberikan ke role **grup** (NOLOGIN); role LOGIN per proses")
    out.append("mewarisinya lewat keanggotaan. Tidak ada DELETE, TRUNCATE, REFERENCES,")
    out.append("atau TRIGGER untuk role mana pun.")
    out.append("")
    out.append("| Objek | Role | Hak |")
    out.append("|---|---|---|")
    for obj in sorted(grants):
        for role, privileges in sorted(set(grants[obj])):
            out.append(f"| `{obj}` | `{role}` | `{privileges}` |")
    out.append("")
    out.append("`schema_migrations` tidak muncul di tabel itu dengan sengaja: hanya")
    out.append("pemilik objek yang boleh menyentuhnya.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hasilkan docs/schema.md dari migrations/.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="jangan menulis; keluar 1 bila docs/schema.md tidak sesuai migrasi",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    expected = render(root / "migrations")
    target = root / "docs" / "schema.md"

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != expected:
            print(
                "docs/schema.md tidak sesuai migrations/. Jalankan:\n"
                "    uv run --no-sync python scripts/generate_schema_doc.py",
                file=sys.stderr,
            )
            return 1
        print("docs/schema.md sesuai migrations/.")
        return 0

    target.write_text(expected, encoding="utf-8", newline="\n")
    print(f"ditulis: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
