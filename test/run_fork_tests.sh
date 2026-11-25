#!/bin/bash
# MintBeacon fork test suite — runs against local anvil BSC fork on :8545.
# Logs everything to /tmp/fork_test.log. Safe to re-run; deploys fresh.
export PATH="$PATH:/home/hatch/.foundry/bin"
RPC=http://localhost:8545
PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f3ff80
SENDER=0x7f225583E3bCE24E0DEaA116EcFF1E552Cb74272
TOSIN=0xF59BC28F2c942a69417652D9359602A755ECCC99
REG=0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F
LOG=/tmp/fork_test.log
exec > >(tee -a $LOG) 2>&1
echo "=== MintBeacon fork test $(date -u +%FT%TZ) ==="

cast rpc --rpc-url $RPC anvil_setBalance $SENDER 0x56BC75E2D63100000 >/dev/null
cast rpc --rpc-url $RPC anvil_setBalance $TOSIN 0xDE0B6B3A7640000 >/dev/null
cast rpc --rpc-url $RPC anvil_impersonateAccount $TOSIN >/dev/null

MBDIR=~/workspace/goals/agent-id-hackathon-build/mintbeacon
DATA=$(cat $MBDIR/deploy_data.txt | cut -c3-)
OUT=$(timeout 90 cast send --rpc-url $RPC --private-key $PK --gas-limit 3000000 --gas-price 1000000000 --create $DATA 2>&1)
MB=$(echo "$OUT" | grep -i contractAddress | awk '{print $2}')
echo "MintBeacon deployed: $MB"
if [ -z "$MB" ]; then echo "DEPLOY FAILED:"; echo "$OUT"; exit 1; fi
echo "$MB" > /tmp/mb_addr.txt
DEPLOY_TX=$(echo "$OUT" | grep -i transactionHash | awk '{print $2}')
cast receipt --rpc-url $RPC $DEPLOY_TX 2>/dev/null | grep -E "gasUsed|blockNumber"

# Deploy mocks
declare -A MOCKS
for C in MockFreeLane MockWriteMint MockPaidPrice MockGateFirst MockClosed MockNothing MockRegistry; do
  D=$(cat /tmp/mockbin/$C.bin)
  O=$(timeout 90 cast send --rpc-url $RPC --private-key $PK --gas-limit 2000000 --gas-price 1000000000 --create $D 2>&1)
  A=$(echo "$O" | grep -i contractAddress | awk '{print $2}')
  MOCKS[$C]=$A
  echo "mock $C -> $A"
done

echo ""
echo "=== REAL TARGETS (checker = Tosin, impersonated) ==="
check() {
  local NAME=$1 TARGET=$2
  TX=$(timeout 90 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 2000000 --gas-price 1000000000 $MB "checkMint(address)" $TARGET 2>&1 | grep -i transactionHash | awk '{print $2}')
  if [ -z "$TX" ]; then echo "$NAME $TARGET: SEND FAILED"; return; fi
  GAS=$(cast receipt --rpc-url $RPC $TX 2>/dev/null | grep gasUsed | awk '{print $2}')
  REC=$(cast call --rpc-url $RPC $MB "verdictOf(address)(uint8,uint8,uint256,uint256,uint256,uint256)" $TARGET 2>/dev/null)
  echo "$NAME $TARGET tx=$TX gas=$GAS record=$REC"
}
check AgentEcho    0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2
check Mossminds    0x5b49b06a2e25fce0bca865be70f3c62b6319367a
check AgentGate    0x42c9ed912025df33ca1676aad4651cdded3f09ed
check GhostyPepe   0x979A49C2fBFa9DEd4bC308de2f54C0E99412A0A4
check AgentPunk    0x51Cfae31cda7D4AD1367473792180E22E13D14FF

echo ""
echo "=== MOCK TARGETS (classifier branches) ==="
check MockFreeLane  ${MOCKS[MockFreeLane]}
check MockWriteMint ${MOCKS[MockWriteMint]}
check MockPaidPrice ${MOCKS[MockPaidPrice]}
check MockGateFirst ${MOCKS[MockGateFirst]}
check MockClosed    ${MOCKS[MockClosed]}
check MockNothing   ${MOCKS[MockNothing]}

echo ""
echo "=== probeMint (view) on real targets ==="
for T in 0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2 0x5b49b06a2e25fce0bca865be70f3c62b6319367a 0x42c9ed912025df33ca1676aad4651cdded3f09ed 0x979A49C2fBFa9DEd4bC308de2f54C0E99412A0A4 0x51Cfae31cda7D4AD1367473792180E22E13D14FF; do
  echo "probe $T: $(cast call --rpc-url $RPC --from $TOSIN $MB "probeMint(address,address)(uint8,uint8,uint256,uint256)" $T $TOSIN 2>&1)"
done

echo ""
echo "=== NEGATIVE TESTS ==="
NONHOLDER=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
cast rpc --rpc-url $RPC anvil_impersonateAccount $NONHOLDER >/dev/null
echo "non-holder checkMint (expect NotAgentHolder revert):"
timeout 60 cast send --rpc-url $RPC --from $NONHOLDER --unlocked --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" 0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2 2>&1 | tail -3
echo "EOA target checkMint from Tosin (expect TargetNotContract revert):"
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" $NONHOLDER 2>&1 | tail -3

echo ""
echo "=== PAUSE TEST (owner = Tosin) ==="
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 500000 --gas-price 1000000000 $MB "pause()" 2>&1 | grep -iE "transactionHash|Error" | head -2
echo "paused? $(cast call --rpc-url $RPC $MB "paused()(bool)")"
echo "checkMint while paused (expect Paused revert):"
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" 0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2 2>&1 | tail -2
echo "non-owner pause attempt (expect NotOwner revert):"
timeout 60 cast send --rpc-url $RPC --from $NONHOLDER --unlocked --gas-limit 500000 --gas-price 1000000000 $MB "pause()" 2>&1 | tail -2
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 500000 --gas-price 1000000000 $MB "unpause()" 2>&1 | grep -iE "transactionHash|Error" | head -2
echo "paused after unpause? $(cast call --rpc-url $RPC $MB "paused()(bool)")"

echo ""
echo "=== COUNTERS ==="
echo "checkerCount (expect 1, only Tosin): $(cast call --rpc-url $RPC $MB "checkerCount()(uint256)")"
echo "totalChecks: $(cast call --rpc-url $RPC $MB "totalChecks()(uint256)")"
echo "=== DONE ==="
