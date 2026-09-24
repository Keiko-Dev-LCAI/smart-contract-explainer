// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  ContractExplainerAccess v2
 * @notice Dollar-pegged pricing: owner sets the LCAI price floor.
 *         Users pay whatever the frontend calculates as the $5 USD
 *         equivalent in LCAI. The contract accepts any amount >= minPrice.
 *
 *         Access is valid for 30 days from payment. Paying again
 *         while still active extends by 30 more days.
 *
 * @dev    Deployed on Lightchain L1 mainnet (chainId 9200).
 *         LCAI is the native coin — no ERC-20 approval needed.
 *         Deploy with Solidity 0.8.20, optimiser ON (200 runs).
 */
contract ContractExplainerAccess {

    // ─────────────────────────────────────────────────────────────
    //  Constants & State
    // ─────────────────────────────────────────────────────────────

    /// @notice Minimum LCAI required to purchase access.
    ///         Owner updates this when the LCAI/USD rate changes significantly.
    ///         Default: 10 LCAI (safety floor — prevents spam, not price-pegged).
    uint256 public minPrice;

    /// @notice How long one payment grants access (30 days in seconds).
    uint256 public constant ACCESS_DURATION = 30 days;

    /// @notice The wallet that can withdraw accumulated LCAI and update price.
    address public owner;

    /// @notice Maps a wallet to the UNIX timestamp when its access expires.
    mapping(address => uint256) public accessExpiry;

    // ─────────────────────────────────────────────────────────────
    //  Events
    // ─────────────────────────────────────────────────────────────

    event AccessPurchased(address indexed buyer, uint256 expiresAt, uint256 amountPaid);
    event Withdrawal(address indexed to, uint256 amount);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event AccessGranted(address indexed wallet, uint256 expiresAt);
    event MinPriceUpdated(uint256 oldPrice, uint256 newPrice);

    // ─────────────────────────────────────────────────────────────
    //  Modifiers
    // ─────────────────────────────────────────────────────────────

    modifier onlyOwner() {
        require(msg.sender == owner, "ContractExplainerAccess: not owner");
        _;
    }

    // ─────────────────────────────────────────────────────────────
    //  Constructor
    // ─────────────────────────────────────────────────────────────

    /**
     * @dev Sets deployer as owner, minPrice to 10 LCAI.
     *      Deployer becomes owner; transfer ownership after deploy if needed.
     */
    constructor() {
        owner    = msg.sender;
        minPrice = 10 ether;   // 10 LCAI safety floor
        emit OwnershipTransferred(address(0), msg.sender);
    }

    // ─────────────────────────────────────────────────────────────
    //  Core: Buy access
    // ─────────────────────────────────────────────────────────────

    /**
     * @notice Send LCAI (>= minPrice) to purchase 30 days of access.
     *
     *         The frontend calculates the LCAI equivalent of $5 USD using
     *         the live LCAI price and sends that amount. This contract just
     *         verifies the amount meets the minimum floor.
     *
     *         • New buyer         → access starts now, expires in 30 days.
     *         • Active buyer      → 30 days added on top of current expiry.
     *         • Lapsed buyer      → starts fresh from now.
     *
     * @dev    Accepts any value >= minPrice. Excess above minPrice is kept
     *         as a donation — no refunds.
     */
    function purchaseAccess() external payable {
        require(
            msg.value >= minPrice,
            "ContractExplainerAccess: insufficient LCAI - check current price"
        );

        uint256 base = (accessExpiry[msg.sender] > block.timestamp)
            ? accessExpiry[msg.sender]
            : block.timestamp;

        uint256 newExpiry = base + ACCESS_DURATION;
        accessExpiry[msg.sender] = newExpiry;

        emit AccessPurchased(msg.sender, newExpiry, msg.value);
    }

    // ─────────────────────────────────────────────────────────────
    //  Views
    // ─────────────────────────────────────────────────────────────

    function hasAccess(address wallet) external view returns (bool) {
        return accessExpiry[wallet] > block.timestamp;
    }

    function getExpiry(address wallet) external view returns (uint256) {
        return accessExpiry[wallet];
    }

    function getBalance() external view returns (uint256) {
        return address(this).balance;
    }

    // ─────────────────────────────────────────────────────────────
    //  Owner: Price management
    // ─────────────────────────────────────────────────────────────

    /**
     * @notice Update the minimum LCAI price floor.
     *         Call this if LCAI pumps hard and you want to raise the floor
     *         (or drops and you want to lower it).
     *         Example: LCAI = $0.10 → $5 = 50 LCAI → setMinPrice(40 ether)
     * @param  _minPrice New minimum in wei (1 LCAI = 1 ether = 10^18 wei).
     */
    function setMinPrice(uint256 _minPrice) external onlyOwner {
        require(_minPrice > 0, "ContractExplainerAccess: price must be > 0");
        emit MinPriceUpdated(minPrice, _minPrice);
        minPrice = _minPrice;
    }

    // ─────────────────────────────────────────────────────────────
    //  Owner: Withdraw
    // ─────────────────────────────────────────────────────────────

    function withdraw() external onlyOwner {
        uint256 bal = address(this).balance;
        require(bal > 0, "ContractExplainerAccess: nothing to withdraw");
        (bool ok, ) = payable(owner).call{value: bal}("");
        require(ok, "ContractExplainerAccess: transfer failed");
        emit Withdrawal(owner, bal);
    }

    function withdrawTo(address payable to, uint256 amount) external onlyOwner {
        require(to != address(0),                "ContractExplainerAccess: zero address");
        require(amount <= address(this).balance, "ContractExplainerAccess: insufficient balance");
        (bool ok, ) = to.call{value: amount}("");
        require(ok, "ContractExplainerAccess: transfer failed");
        emit Withdrawal(to, amount);
    }

    // ─────────────────────────────────────────────────────────────
    //  Owner: Admin helpers
    // ─────────────────────────────────────────────────────────────

    function grantAccess(address wallet, uint256 durationSeconds) external onlyOwner {
        require(wallet != address(0), "ContractExplainerAccess: zero address");
        uint256 base = (accessExpiry[wallet] > block.timestamp)
            ? accessExpiry[wallet]
            : block.timestamp;
        accessExpiry[wallet] = base + durationSeconds;
        emit AccessGranted(wallet, accessExpiry[wallet]);
    }

    function revokeAccess(address wallet) external onlyOwner {
        accessExpiry[wallet] = 0;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ContractExplainerAccess: zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    // ─────────────────────────────────────────────────────────────
    //  Safety
    // ─────────────────────────────────────────────────────────────

    receive() external payable {
        revert("ContractExplainerAccess: use purchaseAccess()");
    }

    fallback() external payable {
        revert("ContractExplainerAccess: use purchaseAccess()");
    }
}
