// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../contracts/MintBeacon.sol";
import "./MockTargets.sol";

/// @title MintBeacon tests (no fork, no mainnet state)
/// @notice Run with: forge test
/// @dev Uses MockRegistry (every address holds an Agent ID) so any test
///      account passes the gate, plus the Mock* targets that exercise each
///      classifier branch. Real-contract verdicts are covered separately by
///      test/simulate_real_contracts.py (eth_call simulation against BSC).
contract MintBeaconTest is Test {
    MintBeacon public beacon;
    MintBeacon public beaconNoHolders;
    MockRegistry public registry;
    MockRegistryNone public registryNone;
    MockFreeLane public freeLane;
    MockWriteMint public writeMint;
    MockPaidPrice public paidPrice;
    MockGateFirst public gateFirst;
    MockClosed public closed;
    MockNothing public nothing;

    address public constant TOSIN = 0xF59BC28F2c942a69417652D9359602A755ECCC99;
    address public constant ALICE = 0x70997970C51812dc3A010C7d01b50e0d17dc79C8;

    function setUp() public {
        registry = new MockRegistry();
        registryNone = new MockRegistryNone();
        beacon = new MintBeacon(address(registry), TOSIN);
        beaconNoHolders = new MintBeacon(address(registryNone), TOSIN);
        freeLane = new MockFreeLane();
        writeMint = new MockWriteMint();
        paidPrice = new MockPaidPrice();
        gateFirst = new MockGateFirst();
        closed = new MockClosed();
        nothing = new MockNothing();
    }

    // --- Classifier branches (caller = this test contract, a "holder") ---

    function testFreeLaneGetterIsFREE() public {
        (uint8 v, uint8 m, uint256 minted, uint256 mx) =
            beacon.probeMint(address(freeLane), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.FREE));
        assertEq(m, uint8(MintBeacon.Method.FREE_LANE_GETTER));
        assertEq(minted, 100);
        assertEq(mx, 1000);
    }

    function testWriteMintIsLikelyFree() public {
        (uint8 v, uint8 m,,) = beacon.probeMint(address(writeMint), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.LIKELY_FREE));
        assertEq(m, uint8(MintBeacon.Method.STATIC_WRITE));
    }

    function testPaidPriceIsPAID() public {
        (uint8 v, uint8 m,,) = beacon.probeMint(address(paidPrice), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.PAID));
        assertEq(m, uint8(MintBeacon.Method.REVERT_PAYMENT));
    }

    function testGateFirstIsLikelyFree() public {
        (uint8 v, uint8 m,,) = beacon.probeMint(address(gateFirst), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.LIKELY_FREE));
        assertEq(m, uint8(MintBeacon.Method.REVERT_GATE));
    }

    function testClosedIsUnknownUnavailable() public {
        (uint8 v, uint8 m,,) = beacon.probeMint(address(closed), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.UNKNOWN));
        assertEq(m, uint8(MintBeacon.Method.UNAVAILABLE));
    }

    function testNothingIsUnknownNone() public {
        (uint8 v, uint8 m,,) = beacon.probeMint(address(nothing), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.UNKNOWN));
        assertEq(m, uint8(MintBeacon.Method.NONE));
    }

    // --- checkMint recording, events, distinct checkers ---

    function testCheckMintRecordsAndCounts() public {
        vm.expectEmit(true, true, false, true, address(beacon));
        emit MintBeacon.MintChecked(
            address(this), address(freeLane), uint8(MintBeacon.Verdict.FREE), 100, 1000, block.timestamp
        );
        beacon.checkMint(address(freeLane));
        (uint8 v, uint8 m, uint256 minted, uint256 mx,, uint256 n) =
            beacon.verdictOf(address(freeLane));
        assertEq(v, uint8(MintBeacon.Verdict.FREE));
        assertEq(m, uint8(MintBeacon.Method.FREE_LANE_GETTER));
        assertEq(minted, 100);
        assertEq(mx, 1000);
        assertEq(n, 1);
        assertEq(beacon.checkerCount(), 1);
        assertEq(beacon.totalChecks(), 1);

        // Same checker again: count grows, distinct checkers do not.
        beacon.checkMint(address(writeMint));
        assertEq(beacon.checkerCount(), 1);
        assertEq(beacon.totalChecks(), 2);

        // A second distinct holder: checkerCount grows.
        vm.prank(ALICE);
        beacon.checkMint(address(freeLane));
        assertEq(beacon.checkerCount(), 2);
        assertEq(beacon.totalChecks(), 3);
    }

    // --- Gate, value, target, pause ---

    function testNonHolderReverts() public {
        vm.expectRevert(abi.encodeWithSelector(MintBeacon.NotAgentHolder.selector, address(this)));
        beaconNoHolders.checkMint(address(freeLane));
    }

    function testValueReverts() public {
        // checkMint is nonpayable, so sending value reverts. The compiler
        // forbids {value:} on a nonpayable call, so use a low-level call.
        vm.deal(address(this), 1 ether);
        (bool ok,) = address(beacon).call{value: 1}(
            abi.encodeWithSelector(MintBeacon.checkMint.selector, address(freeLane))
        );
        assertFalse(ok, "checkMint with value should revert");
        assertEq(beacon.totalChecks(), 0);
    }

    function testEoaTargetReverts() public {
        vm.expectRevert(abi.encodeWithSelector(MintBeacon.TargetNotContract.selector, ALICE));
        beacon.checkMint(ALICE);
    }

    function testZeroAddressReverts() public {
        vm.expectRevert(abi.encodeWithSelector(MintBeacon.TargetNotContract.selector, address(0)));
        beacon.checkMint(address(0));
    }

    function testPauseBlocksChecksButNotReads() public {
        vm.prank(TOSIN);
        beacon.pause();
        assertTrue(beacon.paused());
        vm.expectRevert(MintBeacon.PausedCheck.selector);
        beacon.checkMint(address(freeLane));
        // Reads still work while paused.
        (uint8 v,,,) = beacon.probeMint(address(freeLane), address(this));
        assertEq(v, uint8(MintBeacon.Verdict.FREE));
        vm.prank(TOSIN);
        beacon.unpause();
        assertFalse(beacon.paused());
        beacon.checkMint(address(freeLane));
        assertEq(beacon.totalChecks(), 1);
    }

    function testNonOwnerCannotPause() public {
        vm.expectRevert(abi.encodeWithSelector(MintBeacon.NotOwner.selector, address(this)));
        beacon.pause();
    }

    function testProbeDoesNotRecord() public {
        beacon.probeMint(address(freeLane), address(this));
        assertEq(beacon.totalChecks(), 0);
        assertEq(beacon.checkerCount(), 0);
    }
}
