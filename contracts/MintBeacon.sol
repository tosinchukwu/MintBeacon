// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title MintBeacon — a free-mint verifier for AI agents on BNB Smart Chain
/// @author tosinchukwu.ai — Binance Agent ID Hackathon 2026
/// @notice An agent hands MintBeacon a contract address. MintBeacon answers,
///         live on-chain, whether that contract looks like a FREE, gas-only
///         Agent ID mint — without spending the caller's funds and without
///         minting anything.
///
///         Two ways to use it:
///         - `probeMint(target, checker)` — free view call, no gas, no
///           recording. Anyone can call it off-chain (eth_call).
///         - `checkMint(target)` — state-changing, gas only (value is always
///           0). Only Agent ID holders may call it. It records the verdict
///           on-chain and counts the caller toward the distinct-holder count.
///
/// @dev HOW THE VERDICT IS REACHED (read this before trusting a verdict)
///
///      MintBeacon never mints from the target and never sends it value.
///      It probes the target with STATICCALLs (read-only execution) and
///      classifies what comes back:
///
///      1. Free-lane getters — `canFreeMint(checker)`, `freeLeft(checker)`,
///         `freeRemainingOf(checker)`, `freeFor(checker)`. If one answers
///         "yes / more than 0", the checker personally has a free mint
///         open right now. Verdict: FREE. This is the strongest signal.
///      2. Price getters — `priceFor(checker)`, `MINT_PRICE()`, `PRICE()`,
///         `price()`, `currentPrice()`, `basePrice()`, `quote(...)`. If a
///         checker-priced quote is 0, or every price getter that answers
///         returns 0, the mint costs nothing. Verdict: FREE. If a price
///         getter returns > 0 it is recorded as the paid-lane price, but it
///         does NOT by itself prove PAID — many contracts have a free lane
///         next to a paid lane (e.g. Mossminds: freeMint() is free while
///         PRICE is 0.0005 BNB for the paid lane).
///      3. Mint-function probes — staticcall `freeMint()`, `mint()`,
///         `mint(uint256)` (qty 1) and `claim()` with value 0, in that
///         order. The first entry point that gives a clear signal decides:
///         - The call SUCCEEDS: the function changed nothing in a static
///           context, so it cannot be a real mint. Ignored, try the next.
///         - It reverts with EMPTY data after burning real gas (>= 1000):
///           the call ran logic (at least a storage read) before reverting
///           with no reason — most likely it reached a state change
///           (SSTORE / LOG) that the static context stopped, with value 0.
///           But a bare revert after a storage read looks identical, so
///           this is only LIKELY_FREE, never FREE. Verdict: LIKELY_FREE.
///         - It reverts with EMPTY data after burning almost no gas
///           (< 1000): the selector almost certainly does not exist on
///           the target (a missing selector reverts at dispatch for ~300
///           gas, since the target is warm by probe time). Try the next
///           entry point.
///         - It reverts WITH data: the revert is classified. A payment
///           error ("BNB kam hai", InsufficientPayment, ...) means the
///           mint charges for this path. Verdict: PAID. A gate / identity
///           error (OnlyWallets, NoAgentID, "eoa only", "needs .ai
///           Agent ID") means the call failed on an identity check, not
///           on payment — the mint may well be free for a real holder,
///           but this probe cannot prove it. Verdict: LIKELY_FREE.
///           A sold-out / closed / already-claimed error means the mint
///           is not available right now. Verdict: UNKNOWN.
///         - Anything else: UNKNOWN.
///
///      HONEST LIMITS — the on-chain probe can PROVE "paid" (a payment
///      error, or a price getter above 0 for the only mint path) but it can
///      only SUGGEST "free". A paid mint whose payment check runs AFTER its
///      identity gate would look LIKELY_FREE to this probe, because the
///      probe (calling from MintBeacon's address, which holds no Agent ID)
///      trips the gate before it ever reaches the payment check. That is why
///      verdicts carry a confidence level (FREE / LIKELY_FREE / PAID /
///      UNKNOWN) instead of a boolean, and why the paid deep-check API
///      (off-chain source verification + simulation from the checker's own
///      address, $0.01 via x402) exists as step two: it resolves exactly the
///      cases this probe can only call LIKELY_FREE. A second ambiguity:
///      when a probed mint function attempts a state change, the EVM
///      consumes ALL gas forwarded to the staticcall — which looks exactly
///      like a genuine out-of-gas from inside the EVM, so a looping or
///      gas-griefing target can also read as LIKELY_FREE. The probe cap
///      (150,000 gas per call) bounds what that costs the checker, and the
///      deep-check API is the backstop for any LIKELY_FREE the checker
///      wants confirmed.
///
///      The probe also cannot see: off-chain allowlists, signature-gated
///      mints (a free mint that needs a server signature looks UNKNOWN
///      here), or time windows that open later.
///
/// @dev WHAT THIS CONTRACT NEVER DOES
///      - No payable functions, no receive/fallback: it can never hold BNB.
///      - No token transfers, no approvals, no minting of anything.
///      - No lookups of wallets by Agent ID name. The only identity question
///        ever asked is "does THIS caller hold an Agent ID?" via the
///        registry's balanceOf — the same check every Agent ID-gated mint
///        already performs. MintBeacon verifies CONTRACTS, never people.
///      - The owner can only pause/unpause new checks (emergency brake).
///        Pausing cannot change, delete or fake any recorded verdict.
interface IAgentIDRegistry {
    /// @notice Number of Agent ID tokens held by `account` (0 or 1).
    function balanceOf(address account) external view returns (uint256);
}

contract MintBeacon {
    // ------------------------------------------------------------------
    // Types and constants
    // ------------------------------------------------------------------

    /// @notice Verdict confidence levels. 0 UNKNOWN, 1 FREE, 2 LIKELY_FREE, 3 PAID.
    /// @dev FREE means the probe found positive proof the mint costs nothing
    ///      for the checker (a free-lane getter answering true, or a price
    ///      getter / checker-priced quote of 0).
    ///      LIKELY_FREE means no payment error was seen but the probe could
    ///      not prove freeness: it was stopped by an identity check, or a
    ///      mint function reverted with empty data after burning real gas
    ///      (it may have reached a state change — but a bare revert after a
    ///      storage read looks identical, so this is only LIKELY_FREE).
    ///      PAID means a payment error or a positive price was observed.
    ///      UNKNOWN means the probe could not decide.
    enum Verdict {
        UNKNOWN,
        FREE,
        LIKELY_FREE,
        PAID
    }

    /// @notice How a verdict was reached (audit trail for every check).
    enum Method {
        NONE,
        FREE_LANE_GETTER, // canFreeMint / freeLeft / freeRemainingOf / freeFor said yes
        PRICE_ZERO, // every price getter that answered returned 0
        STATIC_WRITE, // empty revert after real gas burn (may have reached a state change)
        REVERT_GATE, // stopped by an identity / EOA check, no payment error seen
        REVERT_PAYMENT, // stopped by a payment error
        PRICE_POSITIVE, // a price getter returned > 0 (supports PAID, see notes)
        REVERT_UNKNOWN, // reverted with data we could not classify
        UNAVAILABLE // mint window closed / sold out / already claimed
    }

    /// @notice One recorded check of one target contract.
    struct CheckRecord {
        Verdict verdict;
        Method method;
        uint256 minted;
        uint256 maxSupply;
        uint256 checkedAt;
        uint256 checkCount;
    }

    /// @notice The Agent ID (SPACE ID) registry on BSC.
    /// @dev Immutable: it can never be swapped for a weaker identity contract.
    address public immutable registry;

    /// @notice Deployer/owner (Tosin's wallet). Can only pause/unpause.
    address public immutable owner;

    /// @notice Emergency brake. When true, checkMint reverts; reads still work.
    bool public paused;

    /// @notice Latest recorded check per target contract.
    mapping(address => CheckRecord) public records;

    /// @notice Whether an address has ever recorded a check (distinct holders).
    mapping(address => bool) public hasChecked;

    /// @notice Number of distinct Agent ID holders that have recorded a check.
    /// @dev This is the Innovation Award counter: 30 distinct Agent ID
    ///      addresses interacting with this contract during the submission
    ///      period. It only ever grows.
    uint256 public distinctCheckers;

    /// @notice Total recorded checks (all callers, all targets).
    uint256 public totalChecks;

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------

    /// @notice Emitted for every recorded check.
    event MintChecked(
        address indexed checker,
        address indexed target,
        uint8 verdict,
        uint256 minted,
        uint256 maxSupply,
        uint256 timestamp
    );
    event Paused(address indexed owner);
    event Unpaused(address indexed owner);

    // ------------------------------------------------------------------
    // Errors
    // ------------------------------------------------------------------

    error NotAgentHolder(address account);
    error TargetNotContract(address target);
    error PausedCheck();
    error NotOwner(address caller);
    error ZeroAddress();

    // ------------------------------------------------------------------
    // Function selectors probed on the target (computed, see BUILD_NOTE.md)
    // ------------------------------------------------------------------

    bytes4 private constant SEL_TOTAL_SUPPLY = 0x18160ddd; // totalSupply()
    bytes4 private constant SEL_TOTAL_MINTED = 0xa2309ff8; // totalMinted()
    bytes4 private constant SEL_MAX_SUPPLY = 0xd5abeb01; // maxSupply()
    bytes4 private constant SEL_MAX_SUPPLY_CONST = 0x32cb6b0c; // MAX_SUPPLY()
    bytes4 private constant SEL_MINT_OPEN = 0x24bbd049; // mintOpen()
    bytes4 private constant SEL_CAN_FREE_MINT = 0xd5b78380; // canFreeMint(address)
    bytes4 private constant SEL_FREE_LEFT = 0xeb28217e; // freeLeft(address)
    bytes4 private constant SEL_FREE_REMAINING = 0xceda0c40; // freeRemainingOf(address)
    bytes4 private constant SEL_FREE_FOR = 0xbde5ccc9; // freeFor(address)
    bytes4 private constant SEL_PRICE_FOR = 0xf12a7add; // priceFor(address)
    bytes4 private constant SEL_PRICE_FOR_QTY = 0xd527979a; // priceFor(address,uint256)
    bytes4 private constant SEL_QUOTE_FOR = 0x8f79306e; // quote(address,uint256)
    bytes4 private constant SEL_MINT_PRICE = 0xc002d23d; // MINT_PRICE()
    bytes4 private constant SEL_PRICE_CONST = 0x8d859f3e; // PRICE()
    bytes4 private constant SEL_PRICE = 0xa035b1fe; // price()
    bytes4 private constant SEL_CURRENT_PRICE = 0x9d1b464a; // currentPrice()
    bytes4 private constant SEL_BASE_PRICE = 0xc7876ea4; // basePrice()
    bytes4 private constant SEL_QUOTE = 0xed1bd76c; // quote(uint256)
    bytes4 private constant SEL_MINT = 0x1249c58b; // mint()
    bytes4 private constant SEL_MINT_QTY = 0xa0712d68; // mint(uint256)
    bytes4 private constant SEL_FREE_MINT = 0x5b70ea9f; // freeMint()
    bytes4 private constant SEL_CLAIM = 0x4e71d92d; // claim()

    /// @notice Revert-data selectors. Error(string) = 0x08c379a0, Panic = 0x4e487b71.
    bytes4 private constant SEL_ERROR_STRING = 0x08c379a0;
    bytes4 private constant SEL_PANIC = 0x4e487b71;

    /// @notice Gas cap per probe staticcall. A real mint check costs far less;
    ///         the cap only stops a hostile target from burning the caller's gas.
    uint256 private constant PROBE_GAS = 150_000;
    /// @notice Minimum gas burn for an empty revert to count as "executed
    ///         real logic" (LIKELY_FREE). Below this, the selector likely
    ///         does not exist on the target: a missing selector reverts at
    ///         dispatch for ~168 gas (measured 2026-11-19 on anvil; the
    ///         target is warm by probe time, since the supply reads already
    ///         called it), while any storage read costs >= 2,100 cold /
    ///         100 warm on top of dispatch. NOTE: a mint that reaches a
    ///         state change does NOT stop at ~2,300 gas — the EIP-214
    ///         write-protection error consumes ALL gas forwarded to the
    ///         staticcall (measured: the full 150,000 probe cap), so the
    ///         full-cap burn is the common case for a real open mint, not
    ///         an out-of-gas. This signal is LIKELY_FREE, not FREE — a
    ///         bare revert after a storage read, or a gas-griefing target,
    ///         is indistinguishable from a static-call state-change revert.
    uint256 private constant WRITE_GAS_FLOOR = 1_000;

    /// @param registry_ Agent ID registry on BSC:
    ///        0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F.
    /// @param owner_ Owner address (Tosin's wallet). Only pause/unpause.
    constructor(address registry_, address owner_) {
        if (registry_ == address(0) || owner_ == address(0)) revert ZeroAddress();
        registry = registry_;
        owner = owner_;
    }

    // ------------------------------------------------------------------
    // Public functions
    // ------------------------------------------------------------------

    /// @notice Record an on-chain verdict for `target`. Gas only, value 0.
    /// @dev Only Agent ID holders may call it. The verdict is the probe's
    ///      best answer with its confidence level — see the contract header.
    ///      Reverts if paused, if the caller holds no Agent ID, or if the
    ///      target has no deployed code.
    /// @param target Contract to verify.
    /// @return verdict Verdict confidence (0 UNKNOWN, 1 FREE, 2 LIKELY_FREE, 3 PAID).
    /// @return minted Minted count read from the target (0 if unreadable).
    /// @return maxSupply Supply cap read from the target (0 if unreadable).
    function checkMint(address target)
        external
        returns (uint8 verdict, uint256 minted, uint256 maxSupply)
    {
        if (paused) revert PausedCheck();
        if (IAgentIDRegistry(registry).balanceOf(msg.sender) == 0) {
            revert NotAgentHolder(msg.sender);
        }
        (uint8 v, uint8 m, uint256 minted_, uint256 max_) = _classify(target, msg.sender);

        CheckRecord storage r = records[target];
        r.verdict = Verdict(v);
        r.method = Method(m);
        r.minted = minted_;
        r.maxSupply = max_;
        r.checkedAt = block.timestamp;
        r.checkCount += 1;

        totalChecks += 1;
        if (!hasChecked[msg.sender]) {
            hasChecked[msg.sender] = true;
            distinctCheckers += 1;
        }

        emit MintChecked(msg.sender, target, v, minted_, max_, block.timestamp);
        return (v, minted_, max_);
    }

    /// @notice Free view probe: classify `target` for `checker` without
    ///         recording anything and without needing an Agent ID.
    /// @dev Anyone can call this off-chain (eth_call). The frontend and the
    ///      paid deep-check API use it for instant answers; checkMint is
    ///      for recording a verdict on-chain.
    /// @param target Contract to verify.
    /// @param checker Address the verdict is personalised for (free-lane and
    ///        price getters are read for this address).
    /// @return verdict Verdict confidence (0 UNKNOWN, 1 FREE, 2 LIKELY_FREE, 3 PAID).
    /// @return method How the verdict was reached (see Method enum).
    /// @return minted Minted count read from the target (0 if unreadable).
    /// @return maxSupply Supply cap read from the target (0 if unreadable).
    function probeMint(address target, address checker)
        external
        view
        returns (uint8 verdict, uint8 method, uint256 minted, uint256 maxSupply)
    {
        (uint8 v, uint8 m, uint256 minted_, uint256 max_) = _classify(target, checker);
        return (v, m, minted_, max_);
    }

    /// @notice Read the latest recorded check for a target.
    function verdictOf(address target)
        external
        view
        returns (
            uint8 verdict,
            uint8 method,
            uint256 minted,
            uint256 maxSupply,
            uint256 checkedAt,
            uint256 checkCount
        )
    {
        CheckRecord storage r = records[target];
        return (
            uint8(r.verdict),
            uint8(r.method),
            r.minted,
            r.maxSupply,
            r.checkedAt,
            r.checkCount
        );
    }

    /// @notice Number of distinct Agent ID holders that have recorded a check.
    function checkerCount() external view returns (uint256) {
        return distinctCheckers;
    }

    /// @notice Emergency brake: stop new checks. Owner only. Does not touch
    ///         any recorded verdict.
    function pause() external {
        if (msg.sender != owner) revert NotOwner(msg.sender);
        paused = true;
        emit Paused(msg.sender);
    }

    /// @notice Lift the emergency brake. Owner only.
    function unpause() external {
        if (msg.sender != owner) revert NotOwner(msg.sender);
        paused = false;
        emit Unpaused(msg.sender);
    }

    // ------------------------------------------------------------------
    // Classification core
    // ------------------------------------------------------------------

    /// @dev Runs the full probe sequence for `target` as seen by `checker`.
    ///      Order: code check -> supply reads -> mint window -> free-lane
    ///      getters -> price getters -> mint-function probes.
    function _classify(address target, address checker)
        internal
        view
        returns (uint8 verdict, uint8 method, uint256 minted, uint256 maxSupply)
    {
        if (target == address(0) || target == address(this)) {
            revert TargetNotContract(target);
        }
        if (target.code.length == 0) revert TargetNotContract(target);

        (minted, maxSupply) = _readSupply(target);

        // Mint window: if it reports closed, there is nothing to judge.
        (bool okOpen, bool open) = _tryBool(target, abi.encodeWithSelector(SEL_MINT_OPEN));
        if (okOpen && !open) {
            return (uint8(Verdict.UNKNOWN), uint8(Method.UNAVAILABLE), minted, maxSupply);
        }

        // Free-lane getters: the strongest FREE signal.
        if (_checkFreeLane(target, checker)) {
            return (uint8(Verdict.FREE), uint8(Method.FREE_LANE_GETTER), minted, maxSupply);
        }

        // Price getters.
        (uint8 pv, uint8 pm, uint256 paidPrice) = _checkPrices(target, checker);
        if (pv != uint8(Verdict.UNKNOWN)) {
            return (pv, pm, minted, maxSupply);
        }

        // Mint-function probes, most informative first.
        (uint8 mv, uint8 mm) = _probeMints(target);
        if (mv != uint8(Verdict.UNKNOWN)) {
            return (mv, mm, minted, maxSupply);
        }

        // A positive price was seen but no entry point proved anything:
        // the contract charges for at least one path. Only reached when no
        // free-lane getter fired and no probe gave a free signal.
        if (paidPrice > 0) {
            return (uint8(Verdict.PAID), uint8(Method.PRICE_POSITIVE), minted, maxSupply);
        }
        if (mm != uint8(Method.NONE)) {
            return (uint8(Verdict.UNKNOWN), mm, minted, maxSupply);
        }
        return (uint8(Verdict.UNKNOWN), uint8(Method.NONE), minted, maxSupply);
    }

    /// @dev Best-effort supply reads: totalSupply() else totalMinted(),
    ///      maxSupply() else MAX_SUPPLY(). Returns 0 for anything unreadable.
    function _readSupply(address target)
        internal
        view
        returns (uint256 minted, uint256 maxSupply)
    {
        (bool okTs, uint256 ts) = _tryUint(target, abi.encodeWithSelector(SEL_TOTAL_SUPPLY));
        if (okTs) {
            minted = ts;
        } else {
            (bool okTm, uint256 tm) = _tryUint(target, abi.encodeWithSelector(SEL_TOTAL_MINTED));
            if (okTm) minted = tm;
        }
        (bool okMs, uint256 ms) = _tryUint(target, abi.encodeWithSelector(SEL_MAX_SUPPLY));
        if (okMs) {
            maxSupply = ms;
        } else {
            (bool okMc, uint256 mc) = _tryUint(target, abi.encodeWithSelector(SEL_MAX_SUPPLY_CONST));
            if (okMc) maxSupply = mc;
        }
    }

    /// @dev Free-lane getters for `checker`. True when the checker personally
    ///      has a free mint open right now.
    function _checkFreeLane(address target, address checker) internal view returns (bool) {
        (bool okCfm, bool cfm) = _tryBool(target, abi.encodeWithSelector(SEL_CAN_FREE_MINT, checker));
        if (okCfm && cfm) return true;
        bytes4[3] memory freeQtySels = [SEL_FREE_LEFT, SEL_FREE_REMAINING, SEL_FREE_FOR];
        for (uint256 i = 0; i < freeQtySels.length; i++) {
            (bool okF, uint256 f) = _tryUint(target, abi.encodeWithSelector(freeQtySels[i], checker));
            if (okF && f > 0) return true;
        }
        return false;
    }

    /// @dev Price getters. Returns (FREE, PRICE_ZERO, 0) when a
    ///      checker-priced quote is 0, or every global price getter that
    ///      answers returns 0. Otherwise returns (UNKNOWN, NONE, paidPrice)
    ///      where paidPrice is the first positive price seen (0 if none).
    ///      A positive price never concludes PAID here: a free lane may sit
    ///      next to a paid lane (e.g. Mossminds).
    function _checkPrices(address target, address checker)
        internal
        view
        returns (uint8 verdict, uint8 method, uint256 paidPrice)
    {
        (bool okPf, uint256 pf) = _tryUint(target, abi.encodeWithSelector(SEL_PRICE_FOR, checker));
        if (okPf && pf == 0) {
            return (uint8(Verdict.FREE), uint8(Method.PRICE_ZERO), 0);
        }
        (bool okPfq, uint256 pfq) = _tryUint(
            target,
            abi.encodeWithSelector(SEL_PRICE_FOR_QTY, checker, 1)
        );
        if (okPfq && pfq == 0) {
            return (uint8(Verdict.FREE), uint8(Method.PRICE_ZERO), 0);
        }
        (bool okQf, uint256 qf) = _tryUint(target, abi.encodeWithSelector(SEL_QUOTE_FOR, checker, 1));
        if (okQf && qf == 0) {
            return (uint8(Verdict.FREE), uint8(Method.PRICE_ZERO), 0);
        }
        (uint256 answered, uint256 zeroCount, uint256 paid) = _globalPrices(target);
        if (answered > 0 && zeroCount == answered) {
            return (uint8(Verdict.FREE), uint8(Method.PRICE_ZERO), 0);
        }
        return (uint8(Verdict.UNKNOWN), uint8(Method.NONE), paid);
    }

    /// @dev Global (not checker-specific) price getters. Returns how many
    ///      answered, how many of those were 0, and the first positive price.
    function _globalPrices(address target)
        internal
        view
        returns (uint256 answered, uint256 zeroCount, uint256 paidPrice)
    {
        bytes4[6] memory priceSels = [
            SEL_MINT_PRICE,
            SEL_PRICE_CONST,
            SEL_PRICE,
            SEL_CURRENT_PRICE,
            SEL_BASE_PRICE,
            SEL_QUOTE
        ];
        for (uint256 i = 0; i < priceSels.length; i++) {
            bytes memory callData;
            if (priceSels[i] == SEL_QUOTE) {
                callData = abi.encodeWithSelector(SEL_QUOTE, uint256(1));
            } else {
                callData = abi.encodeWithSelector(priceSels[i]);
            }
            (bool okP, uint256 p) = _tryUint(target, callData);
            if (okP) {
                answered += 1;
                if (p == 0) zeroCount += 1;
                else if (paidPrice == 0) paidPrice = p;
            }
        }
    }

    /// @dev Staticcall probes of the mint entry points, most informative
    ///      first: freeMint() -> mint() -> mint(uint256, 1) -> claim().
    ///      The first definitive signal wins. Returns (UNKNOWN, NONE) when
    ///      nothing conclusive was observed, or (UNKNOWN, seenMethod) when
    ///      only unclassifiable reverts were seen.
    /// @dev FULL-CAP BURN: when a probed mint function attempts a state
    ///      change (SSTORE / LOG / CREATE), the EVM aborts the staticcall
    ///      AND consumes all of the gas forwarded to it (this is how both
    ///      geth and revm implement the EIP-214 write-protection error).
    ///      Measured 2026-11-19 on anvil: MockWriteMint.mint() burns the
    ///      entire 150,000 probe cap with empty revert data. From inside
    ///      the EVM that is indistinguishable from a genuine out-of-gas
    ///      (a looping / gas-griefing target also burns the full cap with
    ///      empty data). So an empty revert that burned >= WRITE_GAS_FLOOR
    ///      — including a full-cap burn — is read as "ran logic, most
    ///      likely reached a state change" and returns LIKELY_FREE, never
    ///      FREE. A gas-griefing target is the known false-positive case
    ///      for LIKELY_FREE; the paid deep-check API resolves it off-chain.
    ///      The probe cap (150,000) bounds what a griefing target can cost
    ///      the checker, and real mint functions are far smaller.
    function _probeMints(address target) internal view returns (uint8 verdict, uint8 method) {
        bytes[4] memory probes = [
            abi.encodeWithSelector(SEL_FREE_MINT),
            abi.encodeWithSelector(SEL_MINT),
            abi.encodeWithSelector(SEL_MINT_QTY, uint256(1)),
            abi.encodeWithSelector(SEL_CLAIM)
        ];
        uint8 seenMethod = uint8(Method.NONE);
        for (uint256 i = 0; i < probes.length; i++) {
            (bool success, bytes memory ret, uint256 gasUsed) = _staticcall(
                target,
                probes[i],
                PROBE_GAS
            );
            if (success) {
                // A mint that changes nothing in static context is not a
                // real mint. Ignore and try the next entry point.
                continue;
            }
            if (ret.length == 0) {
                if (gasUsed >= WRITE_GAS_FLOOR) {
                    // Ran logic (or burned the whole cap on a state-change
                    // attempt) with value 0 and no revert data: most likely
                    // reached a state change after every earlier check
                    // passed. Only LIKELY_FREE: a bare revert after a
                    // storage read, or a gas-griefing target, looks
                    // identical from inside the EVM.
                    return (uint8(Verdict.LIKELY_FREE), uint8(Method.STATIC_WRITE));
                }
                // Almost no gas burned: selector probably does not exist.
                continue;
            }
            (uint8 v, uint8 m) = _classifyRevert(ret);
            if (v == uint8(Verdict.PAID) || v == uint8(Verdict.LIKELY_FREE)) {
                return (v, m);
            }
            if (v == uint8(Verdict.UNKNOWN) && m == uint8(Method.UNAVAILABLE)) {
                return (v, m);
            }
            if (m != uint8(Method.NONE)) seenMethod = m;
        }
        return (uint8(Verdict.UNKNOWN), seenMethod);
    }

    /// @dev Classify revert data from a probe staticcall.
    ///      Returns (verdict, method). Verdict PAID / LIKELY_FREE / UNKNOWN
    ///      (never FREE — a revert cannot prove freeness).
    function _classifyRevert(bytes memory ret)
        internal
        pure
        returns (uint8 verdict, uint8 method)
    {
        if (ret.length < 4) {
            return (uint8(Verdict.UNKNOWN), uint8(Method.REVERT_UNKNOWN));
        }
        // Safe: ret.length >= 4 was just checked, so the first 4 bytes exist.
        // forge-lint: disable-next-line(unsafe-typecast)
        bytes4 sel = bytes4(ret);
        if (sel == SEL_ERROR_STRING) {
            bytes memory reason = _decodeReason(ret);
            if (_containsCI(reason, "sold") || _containsCI(reason, "closed") ||
                _containsCI(reason, "limit") || _containsCI(reason, "claimed") ||
                _containsCI(reason, "gone") || _containsCI(reason, "ended") ||
                _containsCI(reason, "paused") || _containsCI(reason, "not open") ||
                _containsCI(reason, "disabled") || _containsCI(reason, "max"))
            {
                return (uint8(Verdict.UNKNOWN), uint8(Method.UNAVAILABLE));
            }
            if (
                _containsCI(reason, "bnb") || _containsCI(reason, "payment") ||
                _containsCI(reason, "price") || _containsCI(reason, "insufficient") ||
                _containsCI(reason, "fee") || _containsCI(reason, "cost") ||
                _containsCI(reason, "ether") || _containsCI(reason, "fund")
            ) {
                return (uint8(Verdict.PAID), uint8(Method.REVERT_PAYMENT));
            }
            if (
                _containsCI(reason, "agent") || _containsCI(reason, "eoa") ||
                _containsCI(reason, "holder") || _containsCI(reason, "identity") ||
                _containsCI(reason, ".ai")
            ) {
                return (uint8(Verdict.LIKELY_FREE), uint8(Method.REVERT_GATE));
            }
            return (uint8(Verdict.UNKNOWN), uint8(Method.REVERT_UNKNOWN));
        }
        if (sel == SEL_PANIC) {
            return (uint8(Verdict.UNKNOWN), uint8(Method.REVERT_UNKNOWN));
        }
        if (_isGateSelector(sel)) {
            return (uint8(Verdict.LIKELY_FREE), uint8(Method.REVERT_GATE));
        }
        if (_isPaymentSelector(sel)) {
            return (uint8(Verdict.PAID), uint8(Method.REVERT_PAYMENT));
        }
        if (_isAvailSelector(sel)) {
            return (uint8(Verdict.UNKNOWN), uint8(Method.UNAVAILABLE));
        }
        return (uint8(Verdict.UNKNOWN), uint8(Method.REVERT_UNKNOWN));
    }

    /// @dev Known identity / EOA custom-error selectors.
    function _isGateSelector(bytes4 sel) internal pure returns (bool) {
        return (
            sel == 0x91d01199 || // OnlyWallets()
            sel == 0xf947691f || // NoAgentId()
            sel == 0xff592cec || // NoAgentID(address)
            sel == 0x7e838e60 || // NoAgentID()
            sel == 0xd43e8b1d || // NotAgentID()
            sel == 0x0d9ab13f || // NotAgent()
            sel == 0x84a7417d || // AgentRequired()
            sel == 0x73620441 || // OnlyAgent()
            sel == 0x9f8129d1 || // OnlyEOA()
            sel == 0xba092d16 || // NotEOA()
            sel == 0xd26e878f // EOAOnly()
        );
    }

    /// @dev Known payment custom-error selectors.
    function _isPaymentSelector(bytes4 sel) internal pure returns (bool) {
        return (
            sel == 0xcd1c8867 || // InsufficientPayment()
            sel == 0x11011294 || // InsufficientValue()
            sel == 0x8b87dfbb || // PaymentRequired()
            sel == 0x356680b7 || // InsufficientFunds()
            sel == 0x583aa026 || // NotEnoughETH()
            sel == 0x43a8b010 || // PriceMismatch()
            sel == 0x3c6b4b28 || // InvalidPayment()
            sel == 0x0494526a || // PaymentTooLow()
            sel == 0x98d4901c || // WrongValue()
            sel == 0xe2f844a0 || // NotEnoughValue()
            sel == 0xf4d678b8 // InsufficientBalance()
        );
    }

    /// @dev Known sold-out / closed / claimed custom-error selectors.
    function _isAvailSelector(bytes4 sel) internal pure returns (bool) {
        return (
            sel == 0x589ed34b || // MintClosed()
            sel == 0x52df9fe5 || // SoldOut()
            sel == 0x2058b6db || // AlreadyClaimed(address)
            sel == 0x5426a580 || // WalletLimit()
            sel == 0x951b974f || // MintNotOpen()
            sel == 0x8522f177 || // FreeGone()
            sel == 0xd05cb609 || // MaxSupplyReached()
            sel == 0xf6e85041 || // ClaimClosed()
            sel == 0xd7d248ba || // MintPaused()
            sel == 0x9e87fac8 // Paused()
        );
    }

    /// @dev Decode the string inside an Error(string) revert.
    ///      Layout: 0x08c379a0 | offset(32) | length(32) | utf8 bytes.
    function _decodeReason(bytes memory ret) internal pure returns (bytes memory reason) {
        if (ret.length < 68) return new bytes(0);
        uint256 len;
        assembly {
            len := mload(add(ret, 68))
        }
        if (ret.length < 68 + len) return new bytes(0);
        reason = new bytes(len);
        for (uint256 i = 0; i < len; i++) {
            reason[i] = ret[68 + i];
        }
    }

    /// @dev Case-insensitive substring search.
    function _containsCI(bytes memory hay, bytes memory needle) internal pure returns (bool) {
        if (needle.length == 0 || hay.length < needle.length) return false;
        for (uint256 i = 0; i + needle.length <= hay.length; i++) {
            bool ok = true;
            for (uint256 j = 0; j < needle.length; j++) {
                if (_lower(hay[i + j]) != _lower(needle[j])) {
                    ok = false;
                    break;
                }
            }
            if (ok) return true;
        }
        return false;
    }

    /// @dev ASCII lowercase helper.
    function _lower(bytes1 b) internal pure returns (bytes1) {
        if (b >= 0x41 && b <= 0x5A) return bytes1(uint8(b) + 32);
        return b;
    }

    /// @dev Bounded staticcall that reports gas used. View-only: safe for
    ///      the probe path (the target cannot change state through it).
    /// @dev Return data is capped at 1024 bytes (assembly copy) so a hostile
    ///      target cannot return-bomb the checker's gas. Revert reasons and
    ///      custom errors are far shorter than that; anything longer is
    ///      unclassifiable anyway.
    /// @dev forge-lint: disable-next-line(calls-loop) — probing a list of
    ///      selectors in a loop is the entire point of this contract; each
    ///      call is a read-only staticcall with a hard gas cap.
    function _staticcall(address target, bytes memory data, uint256 gasCap)
        internal
        view
        returns (bool success, bytes memory ret, uint256 gasUsed)
    {
        uint256 gasBefore = gasleft();
        assembly {
            success := staticcall(gasCap, target, add(data, 0x20), mload(data), 0, 0)
            let size := returndatasize()
            if gt(size, 1024) {
                size := 1024
            }
            ret := mload(0x40)
            mstore(ret, size)
            returndatacopy(add(ret, 0x20), 0, size)
            mstore(0x40, add(add(ret, 0x20), size))
        }
        gasUsed = gasBefore - gasleft();
    }

    /// @dev Try a uint256 getter; ok=false when the call fails or the
    ///      return is not exactly 32 bytes.
    function _tryUint(address target, bytes memory data) internal view returns (bool, uint256) {
        (bool success, bytes memory ret, ) = _staticcall(target, data, 50_000);
        if (!success || ret.length != 32) return (false, 0);
        return (true, abi.decode(ret, (uint256)));
    }

    /// @dev Try a bool getter; ok=false when the call fails or the return
    ///      is not exactly 32 bytes.
    function _tryBool(address target, bytes memory data) internal view returns (bool, bool) {
        (bool success, bytes memory ret, ) = _staticcall(target, data, 50_000);
        if (!success || ret.length != 32) return (false, false);
        return (true, abi.decode(ret, (bool)));
    }
}
