"""Pembacaan teks file migrasi: normalisasi, checksum, dan pemeriksaan statik.

Modul ini murni teks: tidak menyentuh database, jaringan, atau jam.

Ia TIDAK memecah file menjadi beberapa statement untuk dieksekusi. Runner
mengirim seluruh isi file sebagai satu string ke Postgres. Lexer di sini hanya
dipakai untuk pemeriksaan statik: memastikan file tidak mengandung kontrol
transaksi tingkat atas, tanpa salah menangkap ``BEGIN``/``END`` yang berada di
dalam blok ``$$ ... $$`` milik plpgsql.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: Kata kunci kontrol transaksi yang dilarang di file migrasi. Runner yang
#: memegang transaksi, satu transaksi per file; ``BEGIN``/``COMMIT`` di dalam
#: file akan memecah atomisitas itu.
#:
#: ``END`` tidak didaftar: tanpa ``BEGIN`` pasangannya ia tidak bisa menutup
#: transaksi, dan ``END`` adalah bagian wajar dari ``CREATE FUNCTION ...
#: BEGIN ATOMIC ... END`` (Postgres 14+).
FORBIDDEN_STATEMENT_STARTS: Final[tuple[tuple[str, ...], ...]] = (
    ("begin",),
    ("start", "transaction"),
    ("commit",),
    ("rollback",),
    ("abort",),
    ("savepoint",),
    ("release",),
    ("set", "transaction"),
)

#: ``BEGIN ATOMIC`` adalah badan fungsi SQL, bukan pembuka transaksi.
_ALLOWED_AFTER_BEGIN: Final = frozenset({"atomic"})

_IDENT_START: Final = re.compile(r"[A-Za-z_]")
_IDENT_CHAR: Final = re.compile(r"[A-Za-z0-9_]")
_WORD: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def normalize_newlines(content: bytes | str) -> str:
    """Normalisasi CRLF dan CR tunggal menjadi LF.

    Checksum migrasi dihitung dari hasil fungsi ini, supaya file yang sama
    menghasilkan checksum yang sama di Windows dan di Linux.
    """
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    return text.replace("\r\n", "\n").replace("\r", "\n")


def checksum(content: bytes | str) -> str:
    """SHA-256 heksadesimal dari isi file setelah normalisasi baris."""
    return hashlib.sha256(normalize_newlines(content).encode("utf-8")).hexdigest()


def blank_out_literals(sql: str) -> str:
    """Ganti komentar, string, dan blok dolar dengan spasi sepanjang aslinya.

    Panjang dan posisi baris dipertahankan supaya nomor baris hasil pemeriksaan
    masih cocok dengan file asli. Baris baru dibiarkan utuh.
    """
    out: list[str] = []
    i = 0
    n = len(sql)

    def blank(segment: str) -> None:
        out.append("".join("\n" if ch == "\n" else " " for ch in segment))

    while i < n:
        ch = sql[i]

        # Komentar baris: -- sampai akhir baris
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            blank(sql[i:end])
            i = end
            continue

        # Komentar blok: /* ... */, boleh bersarang di Postgres
        if sql.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(sql[i:j])
            i = j
            continue

        # Blok dolar: $$ ... $$ atau $tag$ ... $tag$
        if ch == "$":
            tag = _read_dollar_tag(sql, i)
            if tag is not None:
                close = sql.find(tag, i + len(tag))
                j = n if close == -1 else close + len(tag)
                blank(sql[i:j])
                i = j
                continue

        # String berkutip tunggal. Mode E'' juga mengenali \ sebagai escape.
        if ch == "'":
            escaped = _is_e_string(sql, i)
            j = _scan_single_quoted(sql, i, escaped=escaped)
            blank(sql[i:j])
            i = j
            continue

        # Identifier berkutip ganda
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            blank(sql[i:j])
            i = j
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _read_dollar_tag(sql: str, i: int) -> str | None:
    """Kembalikan penanda blok dolar di posisi ``i``, atau None bila bukan."""
    j = i + 1
    if j < len(sql) and sql[j] == "$":
        return "$$"
    if j < len(sql) and _IDENT_START.match(sql[j]):
        j += 1
        while j < len(sql) and _IDENT_CHAR.match(sql[j]):
            j += 1
        if j < len(sql) and sql[j] == "$":
            return sql[i : j + 1]
    return None


def _is_e_string(sql: str, quote_pos: int) -> bool:
    """True bila kutip di ``quote_pos`` dibuka sebagai E'...' (escape string)."""
    if quote_pos == 0:
        return False
    prev = sql[quote_pos - 1]
    if prev not in "eE":
        return False
    return quote_pos - 1 == 0 or not _IDENT_CHAR.match(sql[quote_pos - 2])


def _scan_single_quoted(sql: str, i: int, *, escaped: bool) -> int:
    """Posisi tepat setelah string berkutip tunggal yang dibuka di ``i``."""
    j = i + 1
    n = len(sql)
    while j < n:
        if escaped and sql[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if sql[j] == "'":
            if j + 1 < n and sql[j + 1] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return n


def find_transaction_control(sql: str) -> list[tuple[int, str]]:
    """Cari kontrol transaksi tingkat atas di ``sql``.

    Mengembalikan daftar ``(nomor_baris, kata_kunci)``. Kosong berarti file
    aman dijalankan di dalam transaksi milik runner.
    """
    stripped = blank_out_literals(sql)
    findings: list[tuple[int, str]] = []
    for statement_start, statement in _split_statements(stripped):
        first_word = _WORD.search(statement)
        if first_word is None:
            continue
        words = [w.lower() for w in _WORD.findall(statement)[:3]]
        if words[0] == "begin" and len(words) > 1 and words[1] in _ALLOWED_AFTER_BEGIN:
            continue
        for pattern in FORBIDDEN_STATEMENT_STARTS:
            if tuple(words[: len(pattern)]) == pattern:
                # Baris dihitung dari kata kunci itu sendiri, bukan dari awal
                # potongan: potongan dimulai tepat setelah titik koma
                # sebelumnya, yang biasanya masih di baris sebelumnya.
                keyword_offset = statement_start + first_word.start()
                line = stripped.count("\n", 0, keyword_offset) + 1
                findings.append((line, " ".join(pattern).upper()))
                break
    return findings


def _split_statements(stripped: str) -> list[tuple[int, str]]:
    """Pecah teks yang sudah dibersihkan pada titik koma tingkat atas.

    Hanya untuk pemeriksaan statik. Runner tidak pernah memakai hasil ini
    untuk mengeksekusi apa pun.
    """
    statements: list[tuple[int, str]] = []
    start = 0
    for index, ch in enumerate(stripped):
        if ch == ";":
            statements.append((start, stripped[start:index]))
            start = index + 1
    statements.append((start, stripped[start:]))
    return [(pos, text) for pos, text in statements if text.strip()]
