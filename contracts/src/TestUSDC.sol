// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// @title TestUSDC — token UJI untuk devnet lokal. BUKAN USDC.
/// @notice Kontrak ini hanya untuk devnet lokal Payment Guard (chain id
///         31337). Ia TIDAK ada hubungannya dengan USDC asli, tidak diaudit,
///         dan tidak boleh di-deploy ke jaringan mana pun selain devnet
///         lokal. Alamatnya berubah setiap devnet dibangun ulang.
/// @dev    ERC-20 minimal, sengaja. Yang ada hanya yang dipakai Payment
///         Guard: enam desimal, `mint` khusus owner, dan jalur transfer.
///         Tidak ada pause, blocklist, permit, upgrade, atau burn — setiap
///         fitur tambahan adalah permukaan yang harus dibaca ulang oleh orang
///         yang meninjau devnet ini.
contract TestUSDC {
    /// @notice Nama token. Sengaja menyebut dirinya token uji.
    string public constant name = "Test USDC (devnet lokal)";

    /// @notice Simbol. Bukan "USDC": simbol yang sama akan mengundang salah
    ///         baca di penjelajah blok dan di log.
    string public constant symbol = "tUSDC";

    /// @notice Enam desimal, sama dengan USDC asli.
    /// @dev    Ini satu-satunya kesamaan yang disengaja dengan USDC, dan ia
    ///         wajib: Payment Guard menolak start bila `decimals()` bukan 6,
    ///         dan seluruh aritmetika uangnya memakai base unit 10**6.
    uint8 public constant decimals = 6;

    /// @notice Pemilik kontrak. Hanya ia yang boleh `mint`.
    address public immutable owner;

    uint256 private _totalSupply;
    mapping(address => uint256) private _balances;
    mapping(address => mapping(address => uint256)) private _allowances;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error NotOwner();
    error ZeroAddress();
    error InsufficientBalance(uint256 tersedia, uint256 diminta);
    error InsufficientAllowance(uint256 tersedia, uint256 diminta);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    function totalSupply() external view returns (uint256) {
        return _totalSupply;
    }

    function balanceOf(address account) external view returns (uint256) {
        return _balances[account];
    }

    function allowance(address pemilik, address spender) external view returns (uint256) {
        return _allowances[pemilik][spender];
    }

    /// @notice Cetak token baru. Hanya owner.
    /// @dev    Dipakai skrip devnet untuk mendanai wallet Executor. Tidak ada
    ///         batas pasokan: ini devnet, dan batas pasokan palsu hanya
    ///         menambah hal yang bisa membingungkan tanpa menambah jaminan.
    function mint(address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        _totalSupply += amount;
        _balances[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert ZeroAddress();
        _allowances[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 jatah = _allowances[from][msg.sender];
        if (jatah < amount) revert InsufficientAllowance(jatah, amount);
        // Jatah tak terbatas (type(uint256).max) sengaja TIDAK diperlakukan
        // khusus: perilaku yang lebih sedikit berarti lebih sedikit yang
        // harus dibaca ulang.
        _allowances[from][msg.sender] = jatah - amount;
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) private {
        if (to == address(0)) revert ZeroAddress();
        uint256 saldo = _balances[from];
        if (saldo < amount) revert InsufficientBalance(saldo, amount);
        unchecked {
            _balances[from] = saldo - amount;
            _balances[to] += amount;
        }
        emit Transfer(from, to, amount);
    }
}
