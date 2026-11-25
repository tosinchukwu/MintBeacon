# MintBeacon backend — free verify tier + paid deep-check API

Stdlib-only Python HTTP server. No pip dependencies, nothing to install.

```bash
cd backend
python3 server.py            # http://127.0.0.1:8000
# or: MINTBEACON_PORT=8080 MINTBEACON_ADDRESS=0x... python3 server.py
```

## Endpoints

| Method | Path | Tier | What it does |
|---|---|---|---|
| `GET /` | | — | Service status, including the honest paid-lane status. |
| `GET /verify?contract=0x...&checker=0x...` | free | Calls `probeMint(target, checker)` on the deployed MintBeacon contract first (default address below). If the on-chain probe is unavailable, falls back to the off-chain classifier mirroring `MintBeacon._classify()`. `checker` defaults to the operator wallet. Returns verdict, method, minted/maxSupply, paid price, evidence log, and `source` (`on-chain` or `off-chain`). No signature, no payment, no spend. |
| `POST /deep-check` `{"contract", "payer"}` | paid | Always answers **HTTP 402** with the x402 payment requirements: **$0.01 in U** (EIP-3009, `eip155:56`, asset `0xcE24439F2D9C6a2289F741120FE202248B666666`, amount `10000000000000000` wei = 0.01 U at 18 decimals) to Tosin's wallet `0xF59BC28F2c942a69417652D9359602A755ECCC99`. Also in the `X-PAYMENT-REQUIREMENTS` header. |
| `POST /deep-check/claim` `{"contract", "payer", "txHash"}` | paid | Verifies a settled U payment **by the actual Transfer event on-chain** (U contract log, from=payer, to=payTo, value exactly 0.01 U) and only then returns the deep-check result: probe simulated **from the payer's own address** (so the Agent ID gate passes), Sourcify source-verification status, and risk flags. One txHash = one deep check (replay-guarded in `data/claims.json`). |
| `GET /deep-check/claim?txHash=0x...` | paid | Re-read a recorded claim. |

## The 402 format

```json
{
  "error": "Payment Required",
  "contract": "0x...",
  "payer": "0x...",
  "paymentRequirements": {
    "scheme": "eip3009",
    "network": "eip155:56",
    "asset": "0xcE24439F2D9C6a2289F741120FE202248B666666",
    "amount": "10000000000000000",
    "amountDisplay": "$0.01",
    "payTo": "0xF59BC28F2c942a69417652D9359602A755ECCC99",
    "description": "MintBeacon deep check: source verification + simulation from the payer's own address + risk flags for one contract.",
    "extra": null
  },
  "paidLaneLive": false,
  "mode": "test-mode-local-verification-only",
  "facilitator": "none-configured"
}
```

`extra` is deliberately `null`: Binance B402 requires it copied verbatim from
its `/supported` response, and we have no `/supported` response (see below).
A buyer/facilitator that needs `extra` cannot build a valid B402 payload from
this 402 alone. That is honest, not a bug — the facilitator route is not
wired yet.

## What the deep check returns that the free probe cannot

The on-chain probe can only *suggest* free (LIKELY_FREE) — it probes from
MintBeacon's address, which holds no Agent ID, so it trips the identity gate
first. The deep check simulates from the **payer's own address**, so a real
holder passes the gate and the actual mint path is exercised. It adds:

- **Sourcify source verification** — `exact_match` / `partial_match` / unverified.
- **Risk flags** — `SOURCE_UNVERIFIED`, `NO_READABLE_MINT_PATH`,
  `PAID_LANE_ONLY`, `MINT_CLOSED`, `STATIC_WRITE_AMBIGUITY`. These are
  observations, not a safety guarantee; a clean flag list does not prove a
  contract is safe.

## Payment verification rule (do not weaken it)

**Verify the actual token `Transfer` to the payee, never nonce state alone.**
A payer can burn their own EIP-3009 nonce without paying, so a nonce check
proves nothing. The claim step requires a transaction receipt whose logs
contain `Transfer(payer → payTo, 0.01 U)` emitted by the U contract, with a
successful status. The claim store is keyed by txHash so one payment cannot
re-run the tool twice.

## Payment routes — what was tested, what is blocked (2026-11-20)

Tested live from this VM; nothing was applied for, registered, or spent.

### 1. Binance B402 — BLOCKED, no testnet call possible

- Docs: https://developers.binance.com/docs/onchainpay-x402/quick-start
- The quick-start says "start with the Sandbox (Testnet) environment first",
  but the Environments page lists **both** Sandbox and Production base URLs
  as **"Please contact us for access"** — no public base URL exists for
  either environment. Every authenticated endpoint (`POST
  /papi/v2/b402/{supported,verify,settle}`) needs a registered `clientId` +
  `accessToken` + RSA-SHA256-signed requests + IP whitelisting. **There is
  no unauthenticated testnet call to make** — the attempt stops at "no URL,
  no credentials".
- What Tosin must do: apply for a partner developer account via the form
  linked on the Binance docs page "How to apply partner developer account"
  (https://developers.binance.com/en/docs/products/onchainpay-x402/basics/6.apply-developer-account
  → form at https://forms.gle/aUQvxUETfGMzyTky5). **Apply separately for
  Sandbox and for Production** — credentials are not shared between
  environments. Per environment, provide: business/brand name, contact
  email, an EVM wallet address to receive funds (testnet address for
  Sandbox, mainnet address for Production — Tosin's wallet
  `0xF59BC28F2c942a69417652D9359602A755ECCC99`), an RSA public key
  (keep the private key), IP addresses for whitelisting, and a webhook
  callback URL if applicable. Binance returns `clientId`, `accessToken`,
  and the base URL for that environment. **Do not submit this application
  without Tosin — it needs his business name, email, and RSA key.**
- One B402 surface IS public and was tested live: the **Bazaar discovery
  API** (`https://www.binance.com/bapi/ramp/v1/public/ramp/b402/bazaar/resources`)
  returned HTTP 200 with 1,010 catalogued resources, no auth. It confirms
  the live Bazaar shape and that **U (`0xcE24439…666666`) is an accepted
  B402 asset on BSC mainnet** (e.g. CoinMarketCap's listings endpoint
  accepts `eip3009` / U at `maxAmountRequired` `10000000000000000` =
  $0.01). Discovery is read-only; it does not let us sell or settle.

### 2. b402.ai (separate protocol, not Binance) — REACHABLE, but NOT usable for U

- Docs: https://docs.b402.ai — facilitator managed by the b402 core team,
  live on BNB Chain + Base mainnet, no API key needed for standard
  verify/settle flows ("Most endpoints are public and require no
  authentication").
- Tested 2026-11-20: `GET https://facilitatorv3.b402.ai/api/v1/health` →
  **HTTP 200** `{"status":"healthy","service":"b402-facilitator",
  "network":"bsc","relayer":"0xE91b564EB8DFF305Ff8efA332f84c487b9da5171"}`.
  The relayer address matches the docs' EIP-712 domain. The older hosts
  `facilitator.b402.ai` and `facilitator.b402.network` returned empty
  replies (curl 52 / HTTP 000) — dead.
- `POST https://facilitatorv3.b402.ai/api/v1/verify` with an empty body and
  with a structurally-shaped payload both **timed out after 15 s with no
  bytes received** (two attempts). No successful verify/settle call was
  observed, so the settle path is **unconfirmed**, not working.
- **Token blocker:** b402.ai supports **USDT, USDC, USD1 only on BNB
  Chain** (FAQ table). U is NOT whitelisted, and b402's Relayer only
  settles whitelisted tokens. A $0.01 deep check via b402.ai would have
  to be priced in **USDT (or USDC/USD1)**, not U — or U would need
  whitelisting by the b402 team. The EIP-712 domain is also b402's own
  (`name: 'B402'`, relayer `0xE91b56…5171`), not the token's EIP-3009
  domain, so a b402.ai payment is a different authorization than the
  plain-U transfer the claim step verifies today.
- What would be needed: a successful `/verify` + `/settle` round-trip
  against `facilitatorv3.b402.ai` with a real signed authorization
  (needs a funded test payer wallet — not attempted, nothing spent), and
  a decision to price in USDT instead of U for this route.

### 3. Open x402 (x402.org) + Bazaar — NOT a BSC mainnet route

- The default facilitator (`x402.org/facilitator`) is testnet-only and
  must not be reused for mainnet. No BSC mainnet facilitator is listed
  in the official quickstart. Not pursued further.

### 4. Self-hosted facilitator — UNBUILT

- Feasible in principle: a small service exposing `/supported`, `/verify`,
  `/settle`; the buyer signs an EIP-3009 `transferWithAuthorization` for
  U; the facilitator's relayer wallet submits it and pays the BNB gas.
  Needs a funded relayer wallet + a hosted backend + the same
  Transfer-log verification rule above. Real work, not a shortcut, and
  not built. The backend in this repo is structured so a facilitator
  module can be added later (`payment_requirements()` is the single
  place the 402 is built; the claim step already does the on-chain
  Transfer check a self-hosted settler would need).

**Bottom line: the paid lane is NOT LIVE.** `/deep-check` returns a correct
402 shape and `/deep-check/claim` verifies a real on-chain U Transfer and
returns a real deep-check result, but that is **local verification only /
test mode**. No facilitator settles payments, nothing catalogues in Bazaar,
and no live $0.01 product exists until one of the four routes above is
confirmed working. Do not present it as live.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `MINTBEACON_ADDRESS` | `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` (deployed 2026-11-25) | Deployed MintBeacon contract. |
| `MINTBEACON_RPC_URL` | bsc-dataseed ×3 + rpc-bsc.48.club | Comma-separated BSC RPC URLs. |
| `MINTBEACON_PAY_TO` | `0xF59BC28F2c942a69417652D9359602A755ECCC99` | Deep-check payment recipient (Tosin's wallet). |
| `MINTBEACON_HOST` | `127.0.0.1` | Bind host. |
| `MINTBEACON_PORT` | `8000` | Bind port. |
| `MINTBEACON_CLAIMS_FILE` | `backend/data/claims.json` | Claim store (txHash → deep-check result). |

## Smoke test

```bash
cd backend
python3 server.py & sleep 1
curl -s "http://127.0.0.1:8000/verify?contract=0x42c9ed912025df33ca1676aad4651cdded3f09ed" | head -30
curl -s -X POST http://127.0.0.1:8000/deep-check -H 'Content-Type: application/json' \
  -d '{"contract":"0x42c9ed912025df33ca1676aad4651cdded3f09ed","payer":"0xF59BC28F2c942a69417652D9359602A755ECCC99"}' -w '\nHTTP %{http_code}\n'
# claim with a bogus txHash must be rejected, not paid out:
curl -s -X POST http://127.0.0.1:8000/deep-check/claim -H 'Content-Type: application/json' \
  -d '{"contract":"0x42c9ed912025df33ca1676aad4651cdded3f09ed","payer":"0xF59BC28F2c942a69417652D9359602A755ECCC99","txHash":"0x0000000000000000000000000000000000000000000000000000000000000000"}'
kill %1
```

Expected: `/verify` returns a verdict for AgentGate Badge — from Tosin's
address it is UNKNOWN / UNAVAILABLE (`AlreadyClaimed`, he already holds
#427; minted 572/10,000 at test time), while a fresh checker gets
LIKELY_FREE / REVERT_GATE (the Agent ID gate stops the probe first —
exactly the 2026-11-19 simulation result). Try it:
`curl -s "http://127.0.0.1:8000/verify?contract=0x42c9ed912025df33ca1676aad4651cdded3f09ed&checker=0x1111111111111111111111111111111111111111"`.
`/deep-check` returns HTTP 402; the bogus claim returns 402 "Payment not
verified".
