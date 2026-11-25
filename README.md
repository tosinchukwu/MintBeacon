# MintBeacon

**A free-mint verifier for AI agents on BSC.** Give it a contract address; it answers live on-chain whether that contract is a free, gas-only Agent ID mint.

Built for the **Binance Wallet × BNB Chain Agent ID Hackathon** (submissions close 2026-12-09 08:00 UTC).

## How it works

`MintBeacon.sol` (`contracts/`) is a small, gas-only contract on BSC. An Agent ID holder calls `checkMint(target)` and the contract probes the target live on-chain — no funds spent, nothing minted — and records a verdict:

| Verdict | Meaning |
|---|---|
| `FREE` | Positive proof the mint costs nothing for the checker (a free-lane getter answers true, or a price/quote of 0). |
| `LIKELY_FREE` | No payment error was seen, but freeness could not be proven — the probe was stopped by an identity check, or a mint function reverted with empty data after burning real gas. Treat as "probably free, deep-check recommended". |
| `PAID` | A payment error ("BNB kam hai", `InsufficientPayment`, …) or a positive price was observed. |
| `UNKNOWN` | The probe could not decide (closed / sold out / no readable mint path). |

The probe reads supply (`totalSupply` / `totalMinted`, `maxSupply`), the mint window, free-lane getters, price getters, then static-calls the mint entry points (`freeMint()`, `mint()`, `mint(uint256)`, `claim()`) with value 0. Full design, known limits, and the two bugs found and fixed in testing are in [`BUILD_NOTE.md`](BUILD_NOTE.md).

The contract is gated on the Agent ID registry (`0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`): only Agent ID holders can record a check. `checkerCount()` is the live count of distinct holders who have used it — the hackathon's 30-holder bar, readable straight off the contract.

**What MintBeacon never does:** no payable functions (it can never hold BNB), no token transfers, no approvals, no minting, no lookups of wallets by Agent ID name. The owner (Tosin's wallet) can only pause/unpause new checks. See `BUILD_NOTE.md` §2–§3.

## Test results (2026-11-19)

- `forge test`: **14/14 pass** (mock targets exercise every classifier branch; gate, pause, distinct checkers, value, EOA target).
- Real contracts via eth_call simulation (`test/simulate_real_contracts.py`): **5/5 correct** — AgentEcho, Mossminds, AgentGate Badge → LIKELY_FREE; GhostyPepe, AgentPunk → PAID.
- Deploy gas measured: **1,672,694** (~0.000084 BNB at 0.05 gwei; ~0.000134 BNB at the wallet's MEDIUM 0.08 gwei). `checkMint` costs 61k–302k gas. Details in `BUILD_NOTE.md` §4–§5.

## Paid deep-check API ($0.01 per check)

The on-chain probe can only *suggest* free (LIKELY_FREE). The paid deep-check resolves those cases off-chain: source verification + simulation from the checker's own address, plus risk flags. Payment is $0.01 in a stablecoin (U first) via x402 — the contract holds no payment logic; the facilitator verifies payment off-chain. **Status 2026-11-20: the backend is built (`backend/`, stdlib-only, smoke-tested live in test mode) but the paid lane is NOT live.** Payment routes (see `BUILD_NOTE.md` §6.1): Binance B402 needs a partner account (no public sandbox URL — the application is the gating step, not yet submitted); b402.ai is reachable but does not support U (USDT instead) and its verify endpoint timed out on probe; self-hosted facilitator unbuilt. **No mainnet paid lane is promised until one route is confirmed working.** The B402 Bazaar catalog itself is readable (`GET .../bazaar/resources` → HTTP 200, 1,010 resources) and already lists U on BSC mainnet via eip3009 at exactly $0.01 — the price point is proven in the catalog.

## Live deployment (2026-11-25)

**MintBeacon is deployed on BNB Smart Chain mainnet.**

- **Website:** https://mintbeacon.vercel.app
- **MCP endpoint (Streamable HTTP):** https://mintbeacon-mcp.wisp.uno/mcp (host reactivated; public health check confirmed HTTP 200)
- **MCP Registry listing:** `io.github.tosinchukwu/mintbeacon` version `1.0.1` (published)
- **MCP health check:** https://mintbeacon-mcp.wisp.uno/healthz (confirmed healthy: `ok: true`)
- **Contract:** `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B`
- **Network:** BNB Smart Chain mainnet (chain ID 56)
- **Agent ID registry:** `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`

The contract is source-verified on BscScan. The MCP server exposes three
read-only tools: `mintbeacon_status`, `verify_mint`, and
`free_mints_feed`. It never signs or sends transactions. Remote connection
instructions and local STDIO setup are in [`mcp/README.md`](mcp/README.md).

The paid deep-check API is **not live**; no paid verification is advertised.
The current product focuses on the deployed on-chain verifier, public website,
and read-only MCP agent integration. See [`BUILD_NOTE.md`](BUILD_NOTE.md)
for implementation details, test history, limitations, and remaining work.

### Website wallet support (2026-11-25)

The website now uses a Windows XP-inspired desktop presentation and supports:

- **Injected EVM wallets:** EIP-6963 discovery lets users choose among compatible browser wallets when multiple providers are announced; a legacy `window.ethereum` fallback is retained.
- **Agent-controlled wallets:** AI agents with an authorised wallet tool can use the website's **Copy AI-agent transaction instructions** action. The generated instructions specify BNB Smart Chain (chain ID 56), the Agent ID eligibility check, MintBeacon destination, `checkMint(address)` call, target address, and calldata.
- **Binance Agentic Wallet:** use Binance's official Agentic Wallet Skills through a trusted supported AI agent. It is not presented as a normal website QR-code or browser-extension connection.
- **Transaction safety:** the site checks Agent ID ownership before gas estimation. Wallets without an Agent ID receive an eligibility warning. Eligible transactions are previewed; the user/agent must still approve the transaction through the wallet's normal authorisation flow.

A wallet can submit an on-chain check only if the actual signing address holds an eligible Agent ID. Merely connecting an EOA or using an AI agent does not bypass this contract gate. WalletConnect support is not claimed unless separately integrated and verified.

## Repo layout

```
contracts/MintBeacon.sol   # the contract (Solidity ^0.8.20, solc 0.8.24, no dependencies)
contracts/MintBeacon.abi.json  # trimmed ABI (functions + events)
test/                      # forge tests + mock targets + fork/eth_call scripts
frontend/                  # single-page app (index.html, no build step, no dependencies)
mcp/                       # MCP stdio + Streamable HTTP server and offline transport tests
server.json                # Official MCP Registry metadata for the live remote endpoint
backend/                   # paid deep-check API (server.py, stdlib-only): GET /verify, POST /deep-check (402), POST /deep-check/claim
bazaar/                     # B402 Bazaar listing draft (listing.json — DRAFT, do not submit)
foundry.toml               # solc 0.8.24, optimizer 200 runs, evm cancun
deploy_data.txt            # prebuilt creation code + constructor args (regenerate after any contract change)
BUILD_NOTE.md              # registry verification, design, limits, tests, deploy quote, paid flow, deployment blocker, promotion plan
```

## Compile / test / deploy

```bash
forge build            # clean (one informational lint)
forge test             # 14 tests, no fork, no mainnet state
# deploy (only on Tosin's explicit confirmation, with a fresh gas quote):
#   forge create --rpc-url https://bsc-dataseed.bnbchain.org --private-key <key> \
#     contracts/MintBeacon.sol:MintBeacon \
#     --constructor-args 0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F <owner-address>
# then source-verify on BscScan.
```

## Hackathon submission notes

- Contracts must be deployed on BSC, source-verified and open-source on BscScan (MIT — see `LICENSE`).
- Innovation Award needs the product live with **at least 30 Agent ID addresses** having interacted with the submitted contract(s) during the submission period. Track `checkerCount()`.
- Red lines respected: no web QR-code login / pairing codes, no prompts bypassing user confirmation, no extra transfers or unlimited approvals, no "Binance"/"BNB" in the name, no seed phrases / private keys / API secrets collected, no promised returns, no name-to-wallet lookup.
- Full checklist in `BUILD_NOTE.md` §8.

## License

MIT — see `LICENSE`.
© Tosinchukwu 2026
