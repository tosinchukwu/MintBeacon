// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title Mock targets for MintBeacon fork tests
/// @notice These mocks exist ONLY to exercise classifier branches that the
///         real target contracts cannot reach from Tosin's address:
///         - MockFreeLane: free-lane getter answers true (FREE path).
///         - MockWriteMint: mint() has no gate and no payment, just a state
///           change (STATIC_WRITE -> FREE path).
///         - MockPaidPrice: positive price + payment revert (PAID path).
///         - MockGateFirst: mint() reverts with a gate error (LIKELY_FREE).
///         - MockClosed: mintOpen() is false (UNKNOWN / UNAVAILABLE).
///         - MockNothing: no functions at all (UNKNOWN / NONE).
/// @dev Never deployed to mainnet. Test-only.

contract MockFreeLane {
    function totalSupply() external pure returns (uint256) {
        return 100;
    }

    function maxSupply() external pure returns (uint256) {
        return 1000;
    }

    function canFreeMint(address) external pure returns (bool) {
        return true;
    }

    // Never reached by the classifier (free lane short-circuits first).
    function mint() external pure {
        revert("paid");
    }
}

contract MockWriteMint {
    uint256 public count;

    function totalSupply() external view returns (uint256) {
        return count;
    }

    function maxSupply() external pure returns (uint256) {
        return 100;
    }

    // No gate, no payment check: just a state change. In a staticcall this
    // reverts with empty data after burning gas -> FREE / STATIC_WRITE.
    function mint() external {
        count += 1;
    }
}

contract MockPaidPrice {
    function totalSupply() external pure returns (uint256) {
        return 50;
    }

    function maxSupply() external pure returns (uint256) {
        return 1000;
    }

    function MINT_PRICE() external pure returns (uint256) {
        return 0.001 ether;
    }

    function mint(uint256) external pure {
        revert("BNB kam hai");
    }
}

contract MockGateFirst {
    error NoAgentID(address caller);

    function totalSupply() external pure returns (uint256) {
        return 10;
    }

    function maxSupply() external pure returns (uint256) {
        return 1000;
    }

    function mint() external view {
        revert NoAgentID(msg.sender);
    }
}

contract MockClosed {
    function totalSupply() external pure returns (uint256) {
        return 10;
    }

    function maxSupply() external pure returns (uint256) {
        return 1000;
    }

    function mintOpen() external pure returns (bool) {
        return false;
    }
}

contract MockNothing {}

/// @notice Registry stand-in for local (non-fork) tests of the distinct
///         checker counting: returns 1 for every address.
contract MockRegistry {
    function balanceOf(address) external pure returns (uint256) {
        return 1;
    }
}

/// @notice Registry stand-in that holds NOBODY: every checkMint reverts
///         with NotAgentHolder. Tests the gate.
contract MockRegistryNone {
    function balanceOf(address) external pure returns (uint256) {
        return 0;
    }
}
