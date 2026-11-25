# MintBeacon MCP server

A free-mint verifier for AI agents on BNB Smart Chain, exposed over MCP
(JSON-RPC 2.0) through **local stdio** and **remote Streamable HTTP**.
Python 3 standard library only — no pip dependencies.

It never signs, never sends transactions, and never holds keys. Everything
it does is read-only: `eth_call`, `eth_getCode`, `eth_estimateGas`.

## Local MCP: stdio

Run the local server from the repository root:

```bash
python3 mcp/server.py
```

The stdio transport is unchanged and remains the simplest option for desktop
agent hosts. It reads newline-delimited JSON-RPC from stdin and writes
responses to stdout; diagnostic messages go to stderr. The server defaults
to the deployed MintBeacon contract. Set an override only when you want to
use another deployment.

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

The deployed contract address is used by default; set `MINTBEACON_ADDRESS` only to override it. Any MCP client that speaks stdio JSON-RPC works — Claude Desktop, MCP
Inspector (`npx @modelcontextprotocol/inspector`), or another local agent host.

## `MINTBEACON_ADDRESS` — deployment status

**DEPLOYED 2026-11-25:** `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` on BSC mainnet,
deployed by Tosin via Remix from osinchukwu.bnb (solc 0.8.24, optimizer OFF).
Owner = his Agentic Wallet, registry = Agent ID registry — verified live
on-chain (see BUILD_NOTE.md §15). The server defaults to this address; set
`MINTBEACON_ADDRESS` (or `--mintbeacon`) to override it.

- **Default (deployed):** `verify_mint` calls `probeMint(target, checker)` on
  `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` and reports the deployed
  contract as the source. The default address is verified on BscScan.
- **Override:** set `MINTBEACON_ADDRESS` or pass `--mintbeacon` to use another
  deployment. If the configured contract cannot be reached, the tool reports
  an MCP error; it does not silently fall back.
- The off-chain classifier remains available in the backend fallback path.

## Tools

### `mintbeacon_status`

Input: no arguments.

Returns the live BSC chain ID, deployed MintBeacon address, Agent ID registry,
source-verification/explorer links, and the on-chain `distinctAgentIdHolders`
and `totalRecordedChecks` counters. This is read-only: calling it does not
increment either counter and does not require a wallet.

### `verify_mint`

Input: `{ "contractAddress": "0x...", "checkerAddress": "0x..." }`.

`checkerAddress` remains optional for backward compatibility, but agents
should **always pass the actual user's or agent's wallet address** when they
want a personalised result. If omitted, the legacy deployer-wallet default
`0xF59BC28F2c942a69417652D9359602A755ECCC99` is used; that does not identify
the MCP caller and may not represent the person asking.

Returns verdict + method + minted/maxSupply + Agent ID gate status
(registry `balanceOf` for the supplied checker) + whether the result came
from the deployed contract or off-chain probe + a `probeLog` of observations.

This is a read-only `eth_call`: it does **not** create a transaction and does
**not** increment MintBeacon's on-chain counters. The optional recorded check
is a separate action on the website and requires the user's own wallet approval.

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

## Remote MCP: Streamable HTTP

The remote transport is implemented in `mcp/http_server.py`. It reuses the
same `handle_request` function and the same three tools as the stdio server;
it does not fork or duplicate the verification logic.

Run locally:

```bash
cd mcp
python3 http_server.py
```

The default listener is `0.0.0.0:8000`. Set `PORT` (preferred by hosting
platforms) or `MCP_PORT` to change the port. The MCP endpoint is
`http://localhost:8000/mcp`; health check is `/healthz`.

### Deploy on Render

A root-level `render.yaml` blueprint creates a Python web service named
`mintbeacon-mcp`, with no third-party packages. In Render, create a new
Blueprint from this GitHub repository and review the service settings before
deploying. The intended public endpoint is:

`https://mintbeacon-mcp.onrender.com/mcp`

The intended health URL is:

`https://mintbeacon-mcp.onrender.com/healthz`

If Render assigns a different hostname, update `MCP_ALLOWED_HOSTS` in the
service environment to that exact hostname (without scheme or path). Keep
the hostname in sync with the deployed URL.

### Remote server environment

- `MINTBEACON_ADDRESS`: deployed MintBeacon contract address.
- `MCP_ALLOWED_HOSTS`: comma-separated exact hostnames accepted by the
  service. The Render blueprint sets the expected `mintbeacon-mcp.onrender.com`.
- `MCP_ALLOWED_ORIGINS`: comma-separated browser origins allowed by CORS.
  Defaults to `https://mintbeacon.vercel.app`. Non-browser MCP clients
  without an `Origin` header can connect without CORS.
- `MCP_RATE_LIMIT_PER_MINUTE`: per-client request limit (default 30).
- `MCP_MAX_BODY_BYTES`: maximum JSON request body (default 65,536 bytes).
- `PORT` or `MCP_PORT`: HTTP listener port (default 8000).

The endpoint is stateless and supports both protocol eras:

- **Modern MCP `2026-07-28`:** `server/discover` plus per-request
  `_meta`; no initialize handshake or session IDs. HTTP POSTs must include
  `MCP-Protocol-Version`, `Mcp-Method`, and (for `tools/call`)
  `Mcp-Name`, with the values validated against the JSON body.
- **Legacy Streamable HTTP `2025-11-25`:** the `initialize` handshake is
  supported, with stateless JSON responses and no issued session ID.

POST requests to `/mcp` receive JSON responses; accepted notifications
receive HTTP 202. GET and DELETE on `/mcp` return 405 because this server
does not offer standalone SSE streams or sessions. This server does not
implement the deprecated `2024-11-05` HTTP+SSE transport. Clients must POST
JSON-RPC requests with `Content-Type: application/json` and an `Accept`
header containing **both** `application/json` and `text/event-stream`.

The HTTP transport applies a request-size limit, per-client in-memory rate
limit, Origin allowlist, and Host allowlist. Local HTTP runs bind to
`127.0.0.1` by default and accept loopback Host values only. The Render
blueprint configures the deployed hostname and the MintBeacon website Origin.
The reverse proxy must overwrite `X-Forwarded-For`; otherwise a caller
could spoof the address used for rate-limiting. Browser clients must use an
allowed Origin. Direct agent clients generally do not send an Origin header.

The local smoke tests run without contacting BSC:

```bash
python3 -m unittest discover -s mcp -p 'test_*.py'
```

### Connect a remote MCP client

Use this remote MCP URL in an MCP client that supports Streamable HTTP:

`https://mintbeacon-mcp.onrender.com/mcp`

For example, an MCP client may accept a remote server entry with transport
type `streamable-http` and that URL. The exact configuration syntax varies
by host. Do not configure a remote URL until the Render service has actually
been deployed and `/healthz`, a modern `server/discover` request, and
`tools/list` have been verified. The legacy `initialize` flow is also
covered by the offline tests.

### Local and remote behavior

Both transports expose the same tools and safety model:

- Local stdio: `python3 mcp/server.py`.
- Remote HTTP: `python3 mcp/http_server.py` (or the Render deployment).
- Neither transport signs, sends, or sponsors transactions.
- `verify_mint` is a read-only `eth_call`; it does not increment on-chain
  hackathon counters.
- Only a successful, separately user-approved website `checkMint`
  transaction can record an on-chain check.

For an agent workflow:

1. Call `mintbeacon_status` to confirm the live BNB Chain deployment.
2. Call `verify_mint` with the target contract and the actual user's/agent's
   own `checkerAddress`.
3. Explain the verdict and evidence; treat `LIKELY_FREE` as a hypothesis,
   not a guarantee.
4. Never claim that an MCP read-only verification counts toward hackathon
   on-chain interaction metrics. Only successful recorded `checkMint`
   transactions update those counters.
5. If the user wants a recorded check, direct them to the website's wallet
   flow. The MCP server itself never signs or sends transactions.

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

Local stdio test record (2026-11-20), driven over JSON-RPC (`initialize` → `tools/list` →
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
- `mintbeacon_status` → live chain/deployment metadata and on-chain counters.
- `tools/list` exposes all three tools with read-only MCP annotations.
- Bad address → JSON-RPC error `-32602`; unknown method → `-32601`;
  `MINTBEACON_ADDRESS` set to an address with no code → clean `isError`.
