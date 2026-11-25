#!/bin/bash
# MintBeacon PLAIN-ANVIL test suite (no fork, no upstream RPC).
# Deploys MintBeacon + MockRegistry + mock targets locally and exercises:
#   every classifier branch, the Agent ID gate, pause, distinct checkers,
#   value-must-be-zero, EOA target, the MintChecked event, gas per check.
# Logs to /tmp/plain_test.log
export PATH="$PATH:/home/hatch/.foundry/bin"
RPC=http://localhost:8545
PK0=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f3ff80
ACCT0=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
ACCT1=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
TOSIN=0xF59BC28F2c942a69417652D9359602A755ECCC99
LOG=/tmp/plain_test.log
exec > >(tee -a $LOG) 2>&1
echo "=== MintBeacon plain-anvil test $(date -u +%FT%TZ) ==="
MBDIR=~/workspace/goals/agent-id-hackathon-build/mintbeacon
SOLC=~/.svm/0.8.24/solc-0.8.24
$SOLC --bin --optimize --optimize-runs 200 $MBDIR/test/MockTargets.sol -o /tmp/mockbin --overwrite >/dev/null 2>&1

gas_of() { # txhash -> gasUsed (decimal); tries JSON receipt, then plain receipt
  local R
  R=$(cast receipt --rpc-url $RPC --json "$1" 2>/dev/null | python3 -c "import json,sys
try:
    d=json.load(sys.stdin); v=d.get('gasUsed')
    print(int(v,16) if isinstance(v,str) else int(v))
except Exception:
    print('')" 2>/dev/null)
  if [ -n "$R" ]; then echo "$R"; return; fi
  cast receipt --rpc-url $RPC "$1" 2>/dev/null | grep -w gasUsed | awk '{print $2}'
}
status_of() { # txhash -> 1 (success) / 0 (reverted) / empty
  cast receipt --rpc-url $RPC --json "$1" 2>/dev/null | python3 -c "import json,sys
try:
    d=json.load(sys.stdin); print(d.get('status'))
except Exception:
    print('')" 2>/dev/null
}
deploy() { # name, binfile, [ctor args hex]
  local NAME=$1 BIN=$2 ARGS=$3
  local D=$(cat $BIN)$ARGS
  local O=$(timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 3000000 --gas-price 1000000000 --create $D 2>&1)
  echo "$O" | grep -i contractAddress | awk '{print $2}'
}
REG=$(deploy MockRegistry /tmp/mockbin/MockRegistry.bin)
REGNONE=$(deploy MockRegistryNone /tmp/mockbin/MockRegistryNone.bin)
echo "registry(all-holders)=$REG registry(none)=$REGNONE"
MB_BIN=$(python3 -c "import json; print(json.load(open('$MBDIR/out/MintBeacon.sol/MintBeacon.json'))['bytecode']['object'])")
MB_ARGS=$(python3 -c "print('$REG'[2:].lower().rjust(64,'0') + '$TOSIN'[2:].lower().rjust(64,'0'))")
MB_OUT=$(timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 3000000 --gas-price 1000000000 --create ${MB_BIN}${MB_ARGS} 2>&1)
MB=$(echo "$MB_OUT" | grep -i contractAddress | awk '{print $2}')
MB_TX=$(echo "$MB_OUT" | grep -i "^transactionHash" | awk '{print $2}')
echo "MintBeacon=$MB (registry=$REG owner=$TOSIN) deployGas=$(gas_of $MB_TX)"
MB2_ARGS=$(python3 -c "print('$REGNONE'[2:].lower().rjust(64,'0') + '$TOSIN'[2:].lower().rjust(64,'0'))")
MB2=$(timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 3000000 --gas-price 1000000000 --create ${MB_BIN}${MB2_ARGS} 2>&1 | grep -i contractAddress | awk '{print $2}')
echo "MintBeacon(no-holder registry)=$MB2"

declare -A M
for C in MockFreeLane MockWriteMint MockPaidPrice MockGateFirst MockClosed MockNothing; do
  M[$C]=$(deploy $C /tmp/mockbin/$C.bin)
  echo "mock $C -> ${M[$C]}"
done

check() { # label, target, from-addr, key-or-IMPERSONATE
  local LABEL=$1 TARGET=$2 FROM=$3 KEY=$4 BEACON=${5:-$MB}
  local OUT TX
  if [ "$KEY" = "IMPERSONATE" ]; then
    OUT=$(timeout 60 cast send --rpc-url $RPC --from $FROM --unlocked --gas-limit 2000000 --gas-price 1000000000 $BEACON "checkMint(address)" $TARGET 2>&1)
  else
    OUT=$(timeout 60 cast send --rpc-url $RPC --private-key $KEY --gas-limit 2000000 --gas-price 1000000000 $BEACON "checkMint(address)" $TARGET 2>&1)
  fi
  TX=$(echo "$OUT" | grep -i "^transactionHash" | awk '{print $2}')
  if [ -z "$TX" ]; then echo "$LABEL: SEND FAILED :: $(echo "$OUT" | tail -2 | tr '\n' ' ')"; return; fi
  local GAS=$(gas_of $TX)
  local REC=$(cast call --rpc-url $RPC $BEACON "verdictOf(address)(uint8,uint8,uint256,uint256,uint256,uint256)" $TARGET 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  echo "$LABEL gas=$GAS record[verdict,method,minted,maxSupply,checkedAt,checkCount]=$REC"
}
echo ""
echo "=== CLASSIFIER BRANCHES (caller A, all-holder registry) ==="
echo "expect: FreeLane FREE/1, WriteMint LIKELY_FREE/3, PaidPrice PAID/5, GateFirst LIKELY_FREE/4, Closed UNKNOWN/8, Nothing UNKNOWN/0"
check MockFreeLane  ${M[MockFreeLane]}  $ACCT0 $PK0
check MockWriteMint ${M[MockWriteMint]} $ACCT0 $PK0
check MockPaidPrice ${M[MockPaidPrice]} $ACCT0 $PK0
check MockGateFirst ${M[MockGateFirst]} $ACCT0 $PK0
check MockClosed    ${M[MockClosed]}    $ACCT0 $PK0
check MockNothing   ${M[MockNothing]}   $ACCT0 $PK0

echo ""
echo "=== EVENT: MintChecked on a fresh MockFreeLane check ==="
EV_TX=$(timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 2000000 --gas-price 1000000000 $MB "checkMint(address)" ${M[MockFreeLane]} 2>&1 | grep -i "^transactionHash" | awk '{print $2}')
EV_TOPIC=$(cast receipt --rpc-url $RPC --json $EV_TX 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['logs'][0]['topics'][0] if d.get('logs') else 'NO LOGS')" 2>/dev/null)
echo "event topic0: $EV_TOPIC"
echo "(expect 0x55a8cb94b7bb7c665dc8296405da08b200a1d3c8b616532784c7984f3abb58b6 = keccak MintChecked(address,address,uint8,uint256,uint256,uint256))"

echo ""
echo "=== probeMint (view) matches checkMint? ==="
for C in MockFreeLane MockWriteMint MockPaidPrice MockGateFirst MockClosed MockNothing; do
  echo "probe $C: $(cast call --rpc-url $RPC --from $ACCT0 $MB "probeMint(address,address)(uint8,uint8,uint256,uint256)" ${M[$C]} $ACCT0 2>&1 | tr '\n' ',' | sed 's/,$//')"
done

echo ""
echo "=== DISTINCT CHECKERS (B = anvil acct 1, impersonated) ==="
echo "checkerCount after A checked 6 targets (expect 1): $(cast call --rpc-url $RPC $MB "checkerCount()(uint256)")"
cast rpc --rpc-url $RPC anvil_impersonateAccount $ACCT1 >/dev/null
check MockFreeLane ${M[MockFreeLane]} $ACCT1 IMPERSONATE
echo "checkerCount after B also checked (expect 2): $(cast call --rpc-url $RPC $MB "checkerCount()(uint256)")"
check MockWriteMint ${M[MockWriteMint]} $ACCT1 IMPERSONATE
echo "checkerCount after B checked a 2nd target (expect still 2): $(cast call --rpc-url $RPC $MB "checkerCount()(uint256)")"
echo "totalChecks: $(cast call --rpc-url $RPC $MB "totalChecks()(uint256)")"

echo ""
echo "=== GATE (no-holder registry instance) ==="
timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 1000000 --gas-price 1000000000 $MB2 "checkMint(address)" ${M[MockFreeLane]} 2>&1 | grep -iE "revertReason|Error" | head -3
echo "(expect NotAgentHolder 0xa81d49b8 above)"

echo ""
echo "=== VALUE MUST BE ZERO ==="
VAL_TX=$(timeout 60 cast send --rpc-url $RPC --private-key $PK0 --value 1 --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" ${M[MockFreeLane]} 2>&1 | grep -i "^transactionHash" | awk '{print $2}')
echo "value=1 tx status: $(status_of $VAL_TX) (expect 0 = reverted, ValueMustBeZero)"

echo ""
echo "=== EOA TARGET ==="
timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" $ACCT1 2>&1 | grep -iE "revertReason|Error" | head -3
echo "(expect TargetNotContract 0x109a6d73 above)"

echo ""
echo "=== PAUSE (owner = Tosin, impersonated) ==="
cast rpc --rpc-url $RPC anvil_setBalance $TOSIN 0xDE0B6B3A7640000 >/dev/null
cast rpc --rpc-url $RPC anvil_impersonateAccount $TOSIN >/dev/null
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 500000 --gas-price 1000000000 $MB "pause()" 2>&1 | grep -iE "transactionHash|Error" | head -2
echo "paused? $(cast call --rpc-url $RPC $MB "paused()(bool)")"
timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 1000000 --gas-price 1000000000 $MB "checkMint(address)" ${M[MockFreeLane]} 2>&1 | grep -iE "revertReason|Error" | head -2
echo "(expect Paused revert above)"
echo "non-owner pause attempt (expect NotOwner revert):"
timeout 60 cast send --rpc-url $RPC --private-key $PK0 --gas-limit 500000 --gas-price 1000000000 $MB "pause()" 2>&1 | grep -iE "revertReason|Error" | head -2
timeout 60 cast send --rpc-url $RPC --from $TOSIN --unlocked --gas-limit 500000 --gas-price 1000000000 $MB "unpause()" 2>&1 | grep -iE "transactionHash|Error" | head -2
echo "paused after unpause? $(cast call --rpc-url $RPC $MB "paused()(bool)")"
echo "checkMint works again after unpause:"
check MockFreeLane ${M[MockFreeLane]} $ACCT0 $PK0
echo "=== DONE ==="
