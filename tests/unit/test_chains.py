"""Pemetaan chain. PRD v2 bagian 6.1, 6.2, dan 7.

Mandate menyimpan string ``"base-sepolia"``; envelope dan kolom
``decisions.chain_id`` memakai integer ``84532``. Pemetaannya hanya ada di
satu tempat, dan tes ini menjaganya tetap begitu.
"""

from __future__ import annotations

import pytest

from payment_guard.chains import (
    BASE_SEPOLIA,
    CHAIN_IDS,
    ChainError,
    chain_id_for,
    is_supported_chain,
    supported_chains,
)


class TestMapping:
    def test_base_sepolia_maps_to_84532(self) -> None:
        """PRD v2 bagian 3: Base Sepolia, chain id 84532."""
        assert CHAIN_IDS["base-sepolia"] == 84532
        assert chain_id_for("base-sepolia") == 84532

    def test_constant_matches_the_key(self) -> None:
        assert BASE_SEPOLIA == "base-sepolia"
        assert BASE_SEPOLIA in CHAIN_IDS

    def test_only_one_chain_is_supported(self) -> None:
        """PRD v2 bagian 2: multi-chain adalah non-tujuan v1."""
        assert supported_chains() == ("base-sepolia",)
        assert len(CHAIN_IDS) == 1

    def test_chain_id_is_int_not_string(self) -> None:
        value = chain_id_for("base-sepolia")
        assert isinstance(value, int)
        assert not isinstance(value, bool)


class TestRejection:
    @pytest.mark.parametrize(
        "name",
        [
            "base-mainnet",
            "ethereum",
            "base",
            "sepolia",
            "Base-Sepolia",
            "BASE-SEPOLIA",
            "base-sepolia ",
            " base-sepolia",
            "base-sepolia\n",
            "",
            "84532",
        ],
    )
    def test_unsupported_names_rejected(self, name: str) -> None:
        assert is_supported_chain(name) is False
        with pytest.raises(ChainError, match="tidak didukung"):
            chain_id_for(name)

    @pytest.mark.parametrize("value", [None, 84532, True, ["base-sepolia"], {"a": 1}])
    def test_non_string_rejected(self, value: object) -> None:
        assert is_supported_chain(value) is False
        with pytest.raises(ChainError):
            chain_id_for(value)  # type: ignore[arg-type]

    def test_message_lists_what_is_supported(self) -> None:
        with pytest.raises(ChainError) as exc:
            chain_id_for("ethereum")
        assert "base-sepolia" in str(exc.value)

    def test_is_supported_does_not_raise(self) -> None:
        assert is_supported_chain(None) is False
        assert is_supported_chain("base-sepolia") is True


class TestNoMagicNumberElsewhere:
    def test_84532_appears_only_in_chains(self) -> None:
        """Angka chain id tidak boleh tersebar sebagai angka ajaib di src/."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "payment_guard"
        offenders = []
        for path in sorted(src.rglob("*.py")):
            if path.name == "chains.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == 84532:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], offenders
