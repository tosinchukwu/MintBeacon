# MintBeacon MCP server

A free-mint verifier for AI agents on BNB Smart Chain, exposed over MCP
(JSON-RPC 2.0, newline-delimited, stdio). Python 3 standard library only —
no pip dependencies, so it runs anywhere Python 3 is installed.

It never signs, never sends transactions, and never holds keys. Everything
it does is read-only: `eth_call`, `eth_getCode`, `eth_estimateGas`.

## Run it

```bash
python3 mcp/server.py
```

The server defaults to the deployed MintBeacon contract. Set an override only when you want to use another deployment.

```bash
MINTBEACON_ADDRESS=0x... python3 mcp/server.py
# or
python3 mcp/server.py --mintbeacon 0x...
```

On startup it prints one line to stderr saying which mode it is in
(deployed contract vs off-chain probe). It exits cleanly (code 0) on EOF
on stdin.

## MCP client config

Generic Claude Desktop style config (`claude_desktop_config.json`).
Adjust the path to where you checked out this repo:

```json
{
  "mcpServers": {
    "mintbeacon": {
      "command": "python3",
      "args": ["/path/to/mintbeacon/mcp/server.py"],
      "env": {
        "MINTBEACON_ADDRESS": "0x..."
      }
    }
  }
}
```

The deployed contract address is used by default; set `MINTBEACON_ADDRESS` only to override it. Any MCP client that speaks stdio JSON-RPC works —
Claude Desktop, MCP Inspector (`npx @modelcontextprotocol/inspector`),
or any agent host.

## `MINTBEACON_ADDRESS` — deployment status

**DEPLOYED 2026-11-25:** `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` on BSC mainnet,
deployed by Tosin via Remix from osinchukwu.bnb (solc 0.8.24, optimizer OFF).
Owner = his Agentic Wallet, registry = Agent ID registry — verified live
on-chain (see BUILD_NOTE.md §15). The server defaults to this address; set
`MINTBEACON_ADDRESS` (or `--mintbeacon`) to override it.

- **Unset (current state):** `verify_mint` runs the **off-chain probe**
  directly against the target contract (code check, supply reads,
  free-lane getters, price getters, value-0 `eth_call` probes of
  `mint()` / `freeMint()` / `mint(uint256)` / `claim()` with revert
  classification). The logic mirrors `test/simulate_real_contracts.py`
  and `contracts/MintBeacon.sol`, and every result is labelled
  `"off-chain probe (MintBeacon contract not yet deployed)"`.
- **Set:** `verify_mint` calls `probeMint(target, checker)` on the
  deployed contract instead, and reports `"deployed MintBeacon contract"`
  as the source (plus `checkerCount` / `totalChecks` when readable).
  If the configured address has no code on BSC, `verify_mint` returns a
  clean MCP error telling you so — it does not silently fall back.

## Tools

### `verify_mint`

Input: `{ "contractAddress": "0x...", "checkerAddress": "0x..." }`
(`checkerAddress` optional, defaults to Tosin's wallet
`0xF59BC28F2c942a69417652D9359602A755ECCC99`).

Returns verdict + method + minted/maxSupply + Agent ID gate status
(registry `balanceOf` for the checker: does this address hold an Agent ID?)
+ whether the result came from the deployed contract or the off-chain probe
+ a `probeLog` of what was observed.

- Verdicts: `UNKNOWN` (0), `FREE` (1), `LIKELY_FREE` (2), `PAID` (3).
- Methods, in contract enum order: `NONE`, `FREE_LANE_GETTER`,
  `PRICE_ZERO`, `STATIC_WRITE`, `REVERT_GATE`, `REVERT_PAYMENT`,
  `PRICE_POSITIVE`, `REVERT_UNKNOWN`, `UNAVAILABLE`.
- **LIKELY_FREE means "probably free — deep-check recommended", NEVER a
  guarantee.**

Bad input (missing/malformed address) returns a JSON-RPC error
(`-32602`). RPC failures return a tool result with `isError: true`
explaining what failed — the server never crashes on a bad call.

### `free_mints_feed`

Input: `{ "limit": 20 }` (optional, default 20, max 100).

Fetches `https://mints-bnb.pages.dev/projects.json` and returns the
`free_live` projects (name, contract address, minted/supply when present).
This is a curated feed, not a MintBeacon verification — run `verify_mint`
on a contract before acting on it. If the fetch fails, or the feed has no
`projects` list, it returns an MCP error saying so. It never invents a list.

## Honest limits

Same as the contract (see `contracts/MintBeacon.sol` header):

- The probe can **prove** "paid" but can only **suggest** "free". A paid
  mint whose payment check runs after its identity gate looks
  `LIKELY_FREE` to this probe, because the probe calls from an address
  that holds no Agent ID and trips the gate first.
- `LIKELY_FREE` is never a guarantee. A gas-griefing target can also read
  as `LIKELY_FREE`. The paid deep-check API (off-chain source verification
  + simulation from the checker's own address, $0.01 via x402) is the
  backstop for any `LIKELY_FREE` worth confirming — not built yet.
- It cannot see off-chain allowlists, signature-gated mints, or time
  windows that open later.
- The off-chain probe calls mint functions from a fixed probe sender that
  holds no Agent ID (the role the MintBeacon contract address plays
  on-chain), so Agent ID-gated mints surface as `LIKELY_FREE /
  REVERT_GATE`. Free-lane and price getters are always read **for the
  checker address**, so a personal free lane still reports `FREE`.
- One adaptation vs the on-chain contract: the contract probes with
  STATICCALLs, so a mint that would succeed reverts with empty data and
  reads as `LIKELY_FREE`. Off-chain we use plain `eth_call`, which
  simulates fully — a mint function that executes successfully with value
  0 is reported as `LIKELY_FREE` (never `FREE`, because the probe sender
  is not the checker).
- No name-to-wallet lookup anywhere. The only identity question ever asked
  is "does THIS checker address hold an Agent ID?" via the registry's
  `balanceOf`.
- The public BSC dataseeds are flaky from some networks (occasional
  truncated responses / HTTP 429). The server batches all read-only
  getters into one JSON-RPC request per verify, retries transport failures
  across four endpoints with exponential backoff (honouring `Retry-After`
  on 429), and reports honestly with `isError` if they all fail.

## Paid deep check

Not built yet. Planned: $0.01 per deep check via x402 in U, MCP + B402
Bazaar discovery. See `BUILD_NOTE.md` §6 for the payment routes (Binance
B402 testnet + partner application first; b402.ai / self-hosted facilitator
as fallback). No mainnet paid lane until one route is confirmed working.

## Tested

2026-11-20, driven over stdio JSON-RPC (`initialize` → `tools/list` →
`tools/call`, clean EOF exit):

- `verify_mint` AgentEcho `0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2` →
  **LIKELY_FREE / REVERT_GATE**, minted 642 / 20,000, checker holds an
  Agent ID (balance 1). Mint reverted `NoAgentId()` (`0xf947691f`).
- `verify_mint` GhostyPepe `0x979A49C2fBFa9DEd4bC308de2f54C0E99412A0A4` →
  **PAID / REVERT_PAYMENT**, minted 65 / 2,222, `MINT_PRICE` 0.001 BNB,
  revert reason `'BNB kam hai'`.
- `verify_mint` Mossminds `0x5b49b06a2e25fce0bca865be70f3c62b6319367a` →
  **LIKELY_FREE / REVERT_GATE**, minted 1,696 / 4,000, revert reason
  `'needs .ai Agent ID'`. (One earlier run of this check hit a transient
  dataseed outage — `IncompleteRead` after all retries — and returned a
  clean `isError`, not a crash; the retry passed.)
- `free_mints_feed` (limit 3) → 3 projects, 46 `free_live` total
  (Worldygg Tree 20,282/99,999; AgentPad Pass 5,959/10,000; M3MO Genesis
  null/10,000).
- Bad address → JSON-RPC error `-32602`; unknown method → `-32601`;
  `MINTBEACON_ADDRESS` set to an address with no code → clean `isError`.
