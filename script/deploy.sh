#!/usr/bin/env bash
# MintBeacon deploy — BSC mainnet, via forge/cast from any funded wallet.
#
# Why this script exists: the Binance Agentic Wallet CLI cannot deploy
# contracts (no contract-creation path, baw 1.12.0), so deployment goes
# through cast. The deployer only pays gas (~0.00013 BNB); the contract
# owner is Tosin's wallet, set by the constructor args already baked
# into deploy_data.txt — the deployer does NOT become the owner.
#
# Usage (on a computer with Foundry installed):
#   export PRIVATE_KEY=0x...        # deployer wallet key — NEVER commit this
#   export RPC_URL=https://bsc-dataseed.bnbchain.org   # optional, this is the default
#   bash script/deploy.sh
#
# Before it sends anything it prints a fresh gas quote and asks for
# confirmation. Nothing is spent until you answer yes.
set -euo pipefail
cd "$(dirname "$0")/.."

RPC_URL="${RPC_URL:-https://bsc-dataseed.bnbchain.org}"
if [ -z "${PRIVATE_KEY:-}" ]; then
  echo "Set PRIVATE_KEY to the deployer wallet private key first: export PRIVATE_KEY=0x..." >&2
  exit 1
fi
command -v cast >/dev/null || { echo "cast not found — install Foundry first: https://getfoundry.sh"; exit 1; }

DATA="$(cat deploy_data.txt)"
case "$DATA" in
  0x0x*) echo "ERROR: deploy_data.txt has a doubled 0x prefix — fix it before deploying."; exit 1 ;;
  0x*) ;;
  *) echo "ERROR: deploy_data.txt must start with 0x."; exit 1 ;;
esac

DEPLOYER="$(cast wallet address --private-key "$PRIVATE_KEY")"
BALANCE_WEI="$(cast balance "$DEPLOYER" --rpc-url "$RPC_URL")"
GAS_PRICE="$(cast gas-price --rpc-url "$RPC_URL")"
# Measured deployment gas: 1,672,694 (BUILD_NOTE.md §5). Cap at 1.8M.
GAS_LIMIT=1800000
COST_WEI=$(( GAS_LIMIT * GAS_PRICE ))

echo "Deployer:  $DEPLOYER"
echo "Balance:   $(cast --to-unit "$BALANCE_WEI" ether) BNB"
echo "Gas price: $GAS_PRICE wei"
echo "Est. cost at cap ($GAS_LIMIT gas): $(cast --to-unit "$COST_WEI" ether) BNB"
echo "Owner (constructor arg): 0xF59BC28F2c942a69417652D9359602A755ECCC99 (Tosin's wallet)"
echo "Registry (constructor arg): 0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F"
read -r -p "Deploy MintBeacon to BSC mainnet? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || { echo "Aborted — nothing sent."; exit 0; }

cast send --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
  --create "$DATA" --gas-limit "$GAS_LIMIT"
echo
echo "Next: source-verify on BscScan, then set MINTBEACON_ADDRESS in"
echo "frontend/index.html, mcp/server.py and backend/server.py (see BUILD_NOTE.md §7)."
