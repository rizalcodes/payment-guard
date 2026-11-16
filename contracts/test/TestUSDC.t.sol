// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {TestUSDC} from "../src/TestUSDC.sol";

/// @notice Potongan antarmuka cheatcode Foundry yang dipakai berkas ini.
/// @dev    Dideklarasikan sendiri, bukan lewat `forge-std`, dan itu disengaja.
///         `forge-std` harus diunduh (`forge install`), dan devnet ini
///         dibangun di mesin yang jaringannya terbatas: solc 0.8.28 sudah ada
///         di dalam image yang dipin, jadi tanpa dependensi eksternal seluruh
///         kompilasi dan tes berjalan offline. Yang dibutuhkan hanya tiga
///         cheatcode, dan menuliskannya sendiri lebih kecil daripada menarik
///         satu pustaka.
interface Vm {
    function prank(address sender) external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert(bytes calldata revertData) external;
    function expectEmit(bool a, bool b, bool c, bool d) external;
}

/// @title Tes kontrak token uji devnet.
/// @dev   Tiga hal yang Payment Guard benar-benar bersandar padanya:
///        `decimals() == 6`, `mint` hanya owner, dan transfer yang memindahkan
///        saldo dengan jumlah yang tepat. Sisanya diuji secukupnya supaya
///        jalur `transferFrom` tidak diam-diam rusak.
///
///        Kegagalan dinyatakan dengan `require`: forge menandai tes gagal
///        ketika ia revert, jadi tidak ada yang perlu ditambahkan.
contract TestUSDCTest {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    TestUSDC internal token;

    address internal pemilik = address(this);
    address internal executor = address(0xE1);
    address internal penerima = address(0xE2);

    /// Satu tUSDC = 10**6 base unit, sama dengan USDC.
    uint256 internal constant SATU = 1_000_000;

    function setUp() public {
        token = new TestUSDC();
    }

    function eq(uint256 ada, uint256 harus, string memory label) internal pure {
        require(ada == harus, label);
    }

    // --- decimals ---------------------------------------------------------

    function test_decimals_is_six() public view {
        eq(uint256(token.decimals()), 6, "decimals harus 6");
    }

    function test_the_startup_check_in_payment_guard_would_pass() public view {
        // Guard menolak start bila decimals() != 6. Ini menyatakan syarat itu
        // sebagai angka, bukan sebagai komentar.
        eq(uint256(token.decimals()), 6, "syarat start Guard");
    }

    function test_symbol_does_not_claim_to_be_usdc() public view {
        // Simbol yang sama dengan USDC akan mengundang salah baca.
        require(
            keccak256(bytes(token.symbol())) == keccak256(bytes("tUSDC")),
            "simbol harus tUSDC, bukan USDC"
        );
    }

    // --- mint hanya owner -------------------------------------------------

    function test_the_owner_can_mint() public {
        token.mint(executor, 20 * SATU);
        eq(token.balanceOf(executor), 20 * SATU, "saldo setelah mint");
        eq(token.totalSupply(), 20 * SATU, "total supply setelah mint");
    }

    function test_a_non_owner_cannot_mint() public {
        vm.prank(executor);
        vm.expectRevert(TestUSDC.NotOwner.selector);
        token.mint(executor, SATU);
    }

    function test_a_failed_mint_changes_nothing() public {
        vm.prank(executor);
        vm.expectRevert(TestUSDC.NotOwner.selector);
        token.mint(executor, SATU);
        eq(token.totalSupply(), 0, "total supply tetap nol");
        eq(token.balanceOf(executor), 0, "saldo tetap nol");
    }

    function test_mint_to_the_zero_address_is_refused() public {
        vm.expectRevert(TestUSDC.ZeroAddress.selector);
        token.mint(address(0), SATU);
    }

    function test_the_owner_is_the_deployer() public view {
        require(token.owner() == pemilik, "owner harus deployer");
    }

    // --- transfer mengurangi saldo ----------------------------------------

    function test_transfer_moves_the_exact_amount() public {
        token.mint(executor, 10 * SATU);

        vm.prank(executor);
        token.transfer(penerima, 4 * SATU);

        eq(token.balanceOf(executor), 6 * SATU, "saldo pengirim berkurang");
        eq(token.balanceOf(penerima), 4 * SATU, "saldo penerima bertambah");
    }

    function test_transfer_does_not_change_total_supply() public {
        token.mint(executor, 10 * SATU);
        uint256 sebelum = token.totalSupply();

        vm.prank(executor);
        token.transfer(penerima, 4 * SATU);

        eq(token.totalSupply(), sebelum, "transfer tidak mencetak uang");
    }

    function test_transfer_emits_the_event_the_watcher_reads() public {
        token.mint(executor, 10 * SATU);

        // Watcher menyaring log Transfer per pengirim; bentuk event-nya adalah
        // apa yang ia baca.
        vm.expectEmit(true, true, false, true);
        emit TestUSDC.Transfer(executor, penerima, 4 * SATU);

        vm.prank(executor);
        token.transfer(penerima, 4 * SATU);
    }

    function test_transfer_beyond_the_balance_is_refused() public {
        token.mint(executor, SATU);

        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(TestUSDC.InsufficientBalance.selector, SATU, 2 * SATU)
        );
        token.transfer(penerima, 2 * SATU);

        eq(token.balanceOf(executor), SATU, "saldo tidak berubah setelah gagal");
    }

    function test_transfer_to_the_zero_address_is_refused() public {
        token.mint(executor, SATU);
        vm.prank(executor);
        vm.expectRevert(TestUSDC.ZeroAddress.selector);
        token.transfer(address(0), SATU);
    }

    // --- approve dan transferFrom -----------------------------------------

    function test_approve_then_transfer_from_spends_the_allowance() public {
        token.mint(executor, 10 * SATU);

        vm.prank(executor);
        token.approve(pemilik, 5 * SATU);
        eq(token.allowance(executor, pemilik), 5 * SATU, "jatah setelah approve");

        token.transferFrom(executor, penerima, 3 * SATU);

        eq(token.balanceOf(executor), 7 * SATU, "saldo pengirim");
        eq(token.balanceOf(penerima), 3 * SATU, "saldo penerima");
        eq(token.allowance(executor, pemilik), 2 * SATU, "jatah berkurang");
    }

    function test_transfer_from_beyond_the_allowance_is_refused() public {
        token.mint(executor, 10 * SATU);

        vm.prank(executor);
        token.approve(pemilik, SATU);

        vm.expectRevert(
            abi.encodeWithSelector(TestUSDC.InsufficientAllowance.selector, SATU, 2 * SATU)
        );
        token.transferFrom(executor, penerima, 2 * SATU);
    }
}
