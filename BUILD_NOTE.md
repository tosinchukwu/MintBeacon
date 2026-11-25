# MintBeacon — build note

**Project:** MintBeacon — a free-mint verifier for AI agents on BSC (Binance Wallet × BNB Chain Agent ID Hackathon).
**Name:** MintBeacon (chosen 2026-11-19 ~20:48 WAT; 9 of 10 candidate names collided — only MintBeacon was clear).
**Status 2026-11-20 ~07:00 WAT:** contract built and tested; **frontend, MCP server, paid deep-check backend and Bazaar draft all built and tested.** **Deployment BLOCKED (see §13) — nothing deployed, nothing spent.** Tosin said "deploy and finish everything" 2026-11-20 ~07:46 WAT, which authorized the deploy; the Binance Agentic Wallet CLI cannot deploy (no contract-creation path), so the deploy needs the §13 alternative (forge/cast from a funded wallet) or waiting for Binance support.
**Update 2026-11-20 ~08:00 WAT:** backend built and smoke-tested live (free `/verify` + 402 + Transfer-verified claim, test mode only — paid lane NOT live); Bazaar listing drafted (DRAFT, not submitted); **deployment is BLOCKED** — `baw contract-call preview` requires a `--to` address and the CLI has no contract-creation path (see §13). Nothing deployed, nothing spent, no application submitted.
**Repo:** `~/workspace/goals/agent-id-hackathon-build/mintbeacon/` (push-ready; Tosin pushes it himself — do not push for him, do not ask for GitHub credentials).

## 1. Verified registry interface (Step 1 — done)

Agent ID registry `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F` is Sourcify **exact_match** (`HandleRegistry`, solc 0.8.24, 67 ABI entries). Holding check confirmed from the verified ABI:

- `balanceOf(address owner) returns (uint256)`, view, selector `0x70a08231` — `> 0` means the address holds an Agent ID.
- Also present: `handleOf(address) returns (string)` (caller's own handle only — MintBeacon never uses it; no name-to-wallet lookup anywhere).

Verified live via BSC RPC 2026-11-19: **Tosin's wallet `0xF59BC28F2c942a69417652D9359602A755ECCC99` returns balanceOf = 1** (Agent ID `tosinchukwu.ai`). BNB balance re-verified 2026-11-19 ~21:00 WAT: **0.001368119485186736 BNB** (unchanged since the 2026-11-19 morning mints).

## 2. Contract design (`contracts/MintBeacon.sol`, Solidity ^0.8.20, solc 0.8.24, self-contained, no dependencies)

- `checkMint(address target)` — state-changing, gas only, value must be 0 (function is nonpayable; sending value reverts). Gated: caller must hold an Agent ID (registry `balanceOf(msg.sender) > 0`, else `NotAgentHolder`). Records the verdict on-chain and emits `MintChecked(checker, target, verdict, minted, maxSupply, timestamp)`.
- `probeMint(address target, address checker)` — free view call, same classification, no gate, no recording. For the frontend's free preview.
- `verdictOf(address target)` — latest recorded check; `checkerCount()` — distinct Agent ID holders who have checked (the 30-holder counter, only ever grows); `totalChecks()`.
- `pause()` / `unpause()` — owner-only emergency brake (owner = Tosin's wallet, immutable). Pausing cannot change, delete or fake any recorded verdict. Reads keep working while paused.
- **What it never does:** no payable functions, no receive/fallback (can never hold BNB); no token transfers, no approvals, no minting; no name-to-wallet lookup — the only identity question is "does THIS caller hold an Agent ID?".
- **Verdicts:** 0 UNKNOWN, 1 FREE, 2 LIKELY_FREE, 3 PAID. **Methods:** NONE, FREE_LANE_GETTER, PRICE_ZERO, STATIC_WRITE, REVERT_GATE, REVERT_PAYMENT, PRICE_POSITIVE, REVERT_UNKNOWN, UNAVAILABLE.
- **Classification order** (in `_classify`): code check → supply reads (`totalSupply()` else `totalMinted()`; `maxSupply()` else `MAX_SUPPLY()`) → `mintOpen()` false → UNKNOWN/UNAVAILABLE → free-lane getters (`canFreeMint(checker)`, `freeLeft`/`freeRemainingOf`/`freeFor(checker) > 0` → FREE) → price getters (checker-priced quote 0, or all answering price getters 0 → FREE; a positive price is recorded but does NOT conclude PAID) → mint-function probes (`freeMint()`, `mint()`, `mint(uint256, 1)`, `claim()`, each a staticcall capped at 150,000 gas) → positive paidPrice with no free signal → PAID/PRICE_POSITIVE → else UNKNOWN.
- **Revert classification:** `Error(string)` keyword-matched (availability → UNKNOWN/UNAVAILABLE; payment → PAID; gate → LIKELY_FREE); known custom-error selectors for gate/payment/availability; Panic and unknown → UNKNOWN/REVERT_UNKNOWN. Empty revert after real gas burn → LIKELY_FREE/STATIC_WRITE (see §3).

## 3. Honest limits (read before trusting a verdict)

- The probe can **prove** "paid" (payment error, or a positive price for the only mint path) but can only **suggest** "free". A paid mint whose payment check runs AFTER its identity gate looks LIKELY_FREE, because the probe calls from MintBeacon's address (holds no Agent ID) and trips the gate first.
- **Full-cap burn ambiguity (found in testing, 2026-11-19):** when a probed mint function attempts a state change, the EVM aborts the staticcall AND consumes **all** gas forwarded to it (EIP-214 write-protection; measured on anvil: a minimal mint burns the entire 150,000 probe cap). From inside the EVM that is indistinguishable from a genuine out-of-gas, so a looping / gas-griefing target can also read as LIKELY_FREE. The probe cap bounds what that costs the checker; the paid deep-check API is the backstop. This is why STATIC_WRITE is LIKELY_FREE, never FREE.
- **Bare-revert ambiguity:** a mint that reverts with no data after a storage read (e.g. `if (claimed[x]) revert();`) looks identical to a static-call state-change revert. Also LIKELY_FREE, never FREE.
- **Off-chain probe gap (found 2026-11-20, coordinator verification):** the backend `GET /verify` and the frontend's off-chain probe use plain `eth_call`, which is *not* static — so for a checker who passes the Agent ID gate, a free mint's `mint()` can execute successfully in the simulation (state discarded) and the probe reports UNKNOWN, where the on-chain probe (STATICCALL) would catch the state-change attempt and report LIKELY_FREE. Verified live: backend `/verify` on AgentEcho with checker=Tosin returned UNKNOWN/NONE (minted 643/20,000), while the MCP server — which probes from a fixed no-Agent-ID sender so the gate trips first — returned LIKELY_FREE/REVERT_GATE for the same contract. The paid deep-check exists precisely to resolve these cases (it simulates from the payer's own address *and* reads the source). Do not present an off-chain UNKNOWN as "not free" — it means "could not decide".
- FREE is reserved for positive evidence only: a free-lane getter answering true for the checker, or a zero price/quote.
- The probe cannot see off-chain allowlists, signature-gated mints (looks UNKNOWN), or future time windows. Verdicts are checker-personalised (e.g. `canFreeMint(Tosin)` is false for Mossminds because he already claimed it; a fresh holder would get FREE).
- **Two bugs found and fixed during testing:**
  1. `WRITE_GAS_FLOOR` was 3,000 — a minimal mint (one SLOAD then SSTORE attempt) burns ~2,300 gas inside the staticcall, below the floor, so it was misread as "selector doesn't exist" (MockWriteMint → UNKNOWN). Fixed: floor is now 1,000 (a missing selector reverts at dispatch for ~168 gas, measured — the target is warm by probe time).
  2. The old code treated a full-cap burn as out-of-gas and skipped it — which made STATIC_WRITE unreachable for real mints (every real mint that reaches a state change burns the full cap). Fixed: the `PROBE_GAS_OOG` skip was removed; an empty revert burning ≥ the floor (including a full-cap burn) returns LIKELY_FREE/STATIC_WRITE. `WRITE_GAS_FLOOR` was also downgraded from FREE to LIKELY_FREE for the bare-revert reason above.

## 4. Test results

### 4a. Mock targets — plain anvil, `forge test` (14/14 pass) + live `cast` run

Run: `forge test` in `mintbeacon/`. Also reproducible live: `test/run_plain_tests.sh` (needs a local `anvil` on :8545).

| Target | Expected | Got (probeMint = checkMint) | Notes |
|---|---|---|---|
| MockFreeLane (`canFreeMint` true) | FREE / FREE_LANE_GETTER | **FREE / FREE_LANE_GETTER** (minted 100, max 1000) | Free-lane short-circuit works |
| MockWriteMint (mint = bare SSTORE) | LIKELY_FREE / STATIC_WRITE | **LIKELY_FREE / STATIC_WRITE** | Full-cap burn path; was UNKNOWN before the §3 fix |
| MockPaidPrice (MINT_PRICE 0.001 + "BNB kam hai") | PAID / REVERT_PAYMENT | **PAID / REVERT_PAYMENT** | |
| MockGateFirst (mint reverts NoAgentID) | LIKELY_FREE / REVERT_GATE | **LIKELY_FREE / REVERT_GATE** | |
| MockClosed (`mintOpen` false) | UNKNOWN / UNAVAILABLE | **UNKNOWN / UNAVAILABLE** | |
| MockNothing (no functions) | UNKNOWN / NONE | **UNKNOWN / NONE** | |

Also verified on plain anvil: non-holder `checkMint` reverts `NotAgentHolder`; value 1 wei reverts (tx status 0, nonpayable); EOA and zero-address targets revert `TargetNotContract`; `pause()` blocks checks (reverts `PausedCheck`) but not reads; non-owner pause reverts `NotOwner`; unpause restores checks; `MintChecked` event emitted with topic `0x55a8cb94b7bb7c665dc8296405da08b200a1d3c8b616532784c7984f3abb58b6`; distinct checkers counted 1 → 2 after a second holder checked, stayed 2 on repeat checks; `totalChecks` counts every check.

`checkMint` gas (plain anvil, at 1 gwei): first check on a target 110k–302k gas (cold storage + full-cap probe burn when STATIC_WRITE fires); repeat checks 61k–228k. Worst case ≈ 0.000024 BNB at the wallet's MEDIUM 0.08 gwei — cheap.

### 4b. Real contracts — eth_call simulation against live BSC (`test/simulate_real_contracts.py`)

The simulation replicates `_classify` exactly (same selectors, same order), calling each target with `from` = the MintBeacon address so `msg.sender` matches the nested staticcall. One caveat: in `eth_call`, `tx.origin == from`, while in the real nested call `tx.origin` is the checker (Tosin, a holder EOA). So the simulation reaches a *later* gate error than the real probe would for AgentEcho (NoAgentId instead of OnlyWallets) and Mossminds ("needs .ai Agent ID" instead of "eoa only") — **same verdict and method in both cases**. A second caveat: `eth_estimateGas` errored on empty reverts, so the simulation could not measure empty-revert gas; that only matters for probes that revert empty, and none of the five real targets do (all revert with data). Live counts at run time (2026-11-19 ~21:00 WAT):

| Contract | Reality | Verdict (checker = Tosin) | Evidence |
|---|---|---|---|
| AgentEcho `0xff2dac…` | free, `mint()` | **LIKELY_FREE / REVERT_GATE** (614 / 20,000) | `mint()` → `NoAgentId()` 0xf947691f (real probe: `OnlyWallets()` first) |
| Mossminds `0x5b49b0…` | free, `freeMint()` | **LIKELY_FREE / REVERT_GATE** (1,638 / 4,000) | `freeMint()` → "needs .ai Agent ID"; PRICE = 0.0005 BNB is the *paid* lane only. `canFreeMint(Tosin)` is false (already claimed) — a fresh holder would get FREE via the getter |
| AgentGate Badge `0x42c9ed…` | free, `mint()` | **LIKELY_FREE / REVERT_GATE** (529 / 10,000) | `mint()` → `NoAgentID(address)` 0xff592cec |
| GhostyPepe `0x979A49…` | PAID 0.001 BNB | **PAID / REVERT_PAYMENT** (64 / 2,222) | `mint(1)` → "BNB kam hai"; MINT_PRICE = 0.001 BNB |
| AgentPunk `0x51Cfae…` | PAID 0.0015 BNB | **PAID / REVERT_PAYMENT** (2.68e27 token units) | `mint(1)` → "not enough BNB"; no maxSupply getter |

**Bottom line:** the on-chain probe correctly separates the three free mints (all LIKELY_FREE, gate-stopped) from the two paid mints (both PAID) — 5/5. It cannot say FREE for the real free mints from Tosin's address, because the probe trips the Agent ID gate before it can prove freeness; that is the documented LIKELY_FREE limitation, and exactly what the paid deep-check API is for (it simulates from the checker's own address).

### 4c. Fork-testing limitation (recorded, not hidden)

Three public BSC RPC endpoints (`bsc-dataseed`, `bsc-dataseed1`, `rpc-bsc.48.club`) could not reliably serve the archive/historical state `anvil` needs to *execute* fork transactions ("missing trie node" / "historical state is not available"). One deployment did land on a fork (see §5), but subsequent fork transactions stuck in the mempool, so the real-contract checks were done via the eth_call simulation above instead of a live fork `checkMint`. If the fork works from a better endpoint later, re-run `test/run_fork_tests.sh` against it — the script is kept for that.

## 5. Deploy gas quote (quote only — NOT deployed)

- Creation bytecode: 7,730 bytes (solc 0.8.24, optimizer 200 runs).
- **Measured deployment gas: 1,672,694** (BSC fork, 2026-11-19; plain-anvil re-measurement: 1,668,804 — same contract, minor env difference).
- Network gas price at quote time: 0.05 gwei. Wallet MEDIUM (what `baw` actually charges): ~0.08 gwei typical, 0.1026 gwei highest seen.
- Cost: **0.0000836 BNB @ 0.05 gwei; 0.0001338 BNB @ 0.08 gwei; 0.0001716 BNB @ 0.1026 gwei.**
- Tosin's balance: **0.00136812 BNB**. Deploy fits easily at any of those prices — worst case ~12.5% of balance, leaving ~0.0012 BNB (roughly 85 more free mints of gas, or ~40 worst-case checkMints). **Re-quote fresh at deploy time** (gas price moves) and show Tosin the number before sending anything.
- Constructor args: registry `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`, owner `0xF59BC28F2c942a69417652D9359602A755ECCC99` (Tosin's wallet). Prebuilt deploy data in `deploy_data.txt` (regenerate after any contract change: it is built from `out/MintBeacon.sol/MintBeacon.json`).

## 6. Paid flow — x402 / B402 routes (research, 2026-11-19 evening)

The deep-check API is $0.01 per check, paid in a stablecoin (U first) via x402. The contract deliberately holds **no payment logic** — payment is verified off-chain by the x402 facilitator; the contract only records checks. Three routes were disentangled (an earlier message conflated them — the correction below is the verified picture):

1. **Binance B402** (developers.binance.com/docs/onchainpay-x402) — Binance's own x402 facilitator. **Live on BSC Testnet; mainnet access is on request** — apply for a partner account and obtain API credentials (`clientId` + `accessToken`). Gas is sponsored, funds move directly buyer → seller. Stablecoins on BSC: U, USDT, USD1, USDC. **So: testable now on testnet, but real $0.01 payments on BSC mainnet need Binance partner onboarding first — plan for that lead time.**
2. **b402.ai** (docs.b402.ai) — a **separate protocol, not Binance's**. Extends x402, **live on BNB Chain mainnet** (USDT, USDC, USD1) and Base, with a facilitator managed by the b402 core team and its own Relayer contract. **This is the concrete mainnet fallback if Binance partner access is slow.** Do not present it as Binance B402.
3. **Open x402 (docs.x402.org) + Bazaar** — the Bazaar discovery extension is real: a resource is catalogued when a payment settles carrying the Bazaar extension; no separate seller registration. But the official quickstart's mainnet path covers Base and Solana; its default facilitator (x402.org/facilitator) is **testnet-only** and must not be reused for mainnet. **No BSC mainnet facilitator is listed there — not a ready BSC mainnet route.**
4. **Self-hosted facilitator** — feasible in principle and documented by independent builders: a small service exposing `/supported`, `/verify`, `/settle`; the buyer signs an EIP-3009 `transferWithAuthorization` (U is EIP-3009 enabled); the facilitator's relayer wallet submits it and pays the BNB gas. **Gotchas from a builder's write-up: verify the actual token `Transfer` to Tosin's wallet, not just the nonce state (the payer can burn their own nonce), and settle only after the tool succeeds, with a claim table keyed by nonce so one payment cannot re-run the tool twice.** This route needs a funded relayer wallet and a hosted backend — real work, not a shortcut. The earlier "another builder did this on BSC with no merchant credentials" claim was not verified.

**Recommended order (no spend yet):**
1. Ship the free tier + MCP first (free checks carry the 30 holders).
2. Test the paid $0.01 flow on Binance B402 **testnet/sandbox**; apply for B402 mainnet partner access in parallel.
3. Evaluate **b402.ai** and a **self-hosted facilitator** as the fallback.
4. **Do not promise a mainnet paid lane until one of those three is confirmed working.**

### 6.1 Paid-route test log — 2026-11-20 (what was actually tested)

Tested live from the VM. Nothing applied for, registered, or spent.
Full detail in `backend/README.md` § "Payment routes".

1. **Binance B402 — BLOCKED, no testnet call possible.** The quick-start
   says "start with Sandbox (Testnet) first", but the Environments page
   lists **both** Sandbox and Production base URLs as *"Please contact us
   for access"* — no public base URL exists for either environment, and
   every `/papi/v2/b402/*` endpoint needs a registered `clientId` +
   `accessToken` + RSA-signed requests + IP whitelisting. There is no
   unauthenticated testnet call to make. What Tosin must do: apply per
   environment via the form linked on the Binance "How to apply partner
   developer account" page (form: https://forms.gle/aUQvxUETfGMzyTky5),
   supplying business name, email, an EVM wallet address (Tosin's wallet
   for Production), an RSA public key, IPs for whitelisting, and a webhook
   URL if applicable. Binance returns `clientId`, `accessToken`, and the
   base URL per environment. **Do not submit without Tosin.**
   One B402 surface IS public and was tested live: the Bazaar discovery
   API (`GET https://www.binance.com/bapi/ramp/v1/public/ramp/b402/bazaar/resources`)
   returned **HTTP 200, 1,010 resources**, no auth — and confirms U
   (`0xcE24439…666666`) is an accepted B402 asset on BSC mainnet
   (CoinMarketCap's listings endpoint accepts `eip3009`/U at
   `maxAmountRequired` `10000000000000000` = $0.01).
2. **b402.ai — reachable, but NOT usable for U.** `GET
   https://facilitatorv3.b402.ai/api/v1/health` → **HTTP 200**
   `{"status":"healthy","service":"b402-facilitator","network":"bsc",
   "relayer":"0xE91b564EB8DFF305Ff8efA332f84c487b9da5171"}` (relayer
   matches the docs' EIP-712 domain). Older hosts `facilitator.b402.ai`
   and `facilitator.b402.network` returned empty replies (HTTP 000).
   `POST /api/v1/verify` (empty body, then a shaped payload) **timed out
   twice at 15 s with no bytes** — settle path unconfirmed. Token
   blocker: b402.ai supports **USDT/USDC/USD1 only on BNB Chain**; U is
   not whitelisted, so a b402.ai deep check would have to be priced in
   USDT, not U, and its EIP-712 domain is b402's own Relayer, not the
   token's EIP-3009 domain. No API key needed for standard flows.
3. **Open x402 (x402.org) + Bazaar — not a BSC mainnet route.** Default
   facilitator is testnet-only; no BSC mainnet facilitator listed.
4. **Self-hosted facilitator — unbuilt.** Feasible (EIP-3009
   `transferWithAuthorization` for U + a relayer wallet paying BNB gas),
   needs a funded relayer + hosted backend. The backend in this repo is
   structured for it: `payment_requirements()` is the single place the
   402 is built, and the claim step already does the on-chain Transfer
   check a self-hosted settler needs.

**Backend built 2026-11-20** (`backend/`, stdlib-only Python):
`GET /verify` free tier (mirrors `_classify`; smoke-tested live —
AgentGate Badge 573/10,000: LIKELY_FREE/REVERT_GATE for a fresh checker,
UNKNOWN/UNAVAILABLE `AlreadyClaimed` for Tosin, who already holds #427),
`POST /deep-check` → HTTP 402 ($0.01 in U, EIP-3009, payTo Tosin's
wallet), `POST /deep-check/claim` → verifies the actual U Transfer event
on-chain (never nonce state alone) and only then returns the deep check
(Sourcify status + simulation from the payer's own address + risk flags),
one txHash = one deep check. **Test mode / local verification only —
the paid lane is NOT live.** U decimals verified on-chain: 18, so $0.01 =
`10000000000000000` wei. One transient note: the first `/verify` attempt
failed with `IncompleteRead` across all four BSC RPC endpoints; the retry
seconds later worked — the known BSC RPC flakiness, not a backend bug.

The paid deep-check resolves exactly the cases the on-chain probe can only call LIKELY_FREE: off-chain source verification (Sourcify) + simulation from the *checker's own address* (so the Agent ID gate passes and the real mint path is exercised), plus risk flags (unverified source, honeypot-ish patterns, no liquidity). The free on-chain check stays free forever — no paid lane in the contract.

## 7. Remaining steps (in order)

1. **Resolve the deployment blocker (§13), then Tosin confirms deploy** with the fresh gas quote (§5). Deploy MintBeacon on BSC, source-verify on BscScan (contract is open-source, MIT — see `LICENSE`). **Deployment via the Binance Agentic Wallet CLI is currently NOT possible** — see §13.
2. **Frontend** (`frontend/` — **built and tested 2026-11-20**): single page — paste a contract address, free off-chain probe while undeployed (or `probeMint` once deployed), then a preview-before-signing `checkMint` call (show exact gas + verdict before he signs). No QR login, no pairing codes, no prompts that bypass confirmation. The on-chain path is tested at the encoding level only; it goes live once the contract is deployed and `MINTBEACON_ADDRESS` is set.
3. **MCP server** (`mcp/` — **built and tested 2026-11-20**): `verify_mint(contractAddress)` + `free_mints_feed`, stdlib-only Python, tested live over stdio against AgentEcho (LIKELY_FREE) and GhostyPepe (PAID). Uses the deployed contract's `probeMint` when `MINTBEACON_ADDRESS` is set; otherwise runs the off-chain probe and says so. Free tier first (free checks carry the 30 holders).
4. **Paid deep-check API** ($0.01 via x402 in U): **backend built 2026-11-20** (`backend/` — free `/verify`, 402, Transfer-verified claim; smoke-tested live, test mode only). Still needed: a confirmed facilitator route — B402 partner application (needs Tosin, see §6.1), or b402.ai priced in USDT (U not supported there), or a self-hosted facilitator. **No mainnet paid lane until one route is confirmed working.**
5. **B402 Bazaar listing**: **draft prepared 2026-11-20** (`bazaar/listing.json` + `bazaar/README.md`, status DRAFT — do not submit). Once a paid flow settles with the Bazaar extension, the resource catalogues automatically — no separate seller registration. (Do not list before the paid flow works.)
6. **Promotion to 30 holders:** see §14 (promotion plan). Deploy + verify, then post the free-check link on X with `#BinanceWalletHackathon` + `#AgenticWallet` and in the Agent ID spaces where free-mint lists circulate. Track `checkerCount()` on-chain and report it. The X-post lane (20 winners share 2,000 USDC, 100 USDC each) only counts for posts about a participating project — post once MintBeacon is live.
7. **Submission** before **2026-12-09 08:00 UTC**: contract deployed + source-verified + open-source, ≥ 30 distinct Agent ID holders interacted, free tier + MCP live, paid flow tested (testnet first; mainnet route confirmed or honestly marked pending).

## 8. Submission checklist

- [x] Name chosen: MintBeacon (no collisions found)
- [x] Agent ID registry interface verified from source-verified contract
- [x] Contract written, compiled clean (solc 0.8.24, one informational lint only), tested (14/14 forge tests + live anvil + real-contract simulation 5/5)
- [x] Deploy gas quoted (fits in Tosin's balance with margin)
- [x] Backend built: free `GET /verify` + `POST /deep-check` (402) + `POST /deep-check/claim` (Transfer-verified), stdlib-only, smoke-tested live 2026-11-20 — **test mode only, paid lane NOT live**
- [x] Bazaar listing drafted (`bazaar/listing.json` + `bazaar/README.md`) — **DRAFT, do not submit** until a paid flow is confirmed working and the contract is deployed
- [x] **DEPLOYED 2026-11-25:** `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` on BSC, deployed by Tosin via Remix from osinchukwu.bnb (see §15). Owner = his Agentic Wallet, registry correct, probeMint verified live. (The 2026-11-20 CLI blocker is resolved — Remix deploy, not the CLI.)
- [x] Source-verified on BscScan — Remix verified it 2026-11-25 (compiler 0.8.24, optimizer OFF, EVM cancun); Sourcify confirms `exact_match` creation + runtime, verifiedAt 2026-11-25T07:35:34Z. See §16.
- [x] Frontend single page live (preview-before-signing) — **built and tested 2026-11-20** (18/18 logic + 15/15 DOM harness checks, live BSC verdicts correct); `MINTBEACON_ADDRESS` set 2026-11-25, on-chain `checkMint` path now live
- [x] MCP server live (`mintbeacon.verify` + `free_mints_feed`) — **built and tested 2026-11-20** over stdio against live BSC; deployed-contract path re-tested live 2026-11-25 (AgentEcho + Glitchronin → LIKELY_FREE, source "deployed MintBeacon contract")
- [x] Backend free `/verify` now uses the deployed contract's probeMint (on-chain first, off-chain fallback) — re-tested live 2026-11-25, matches MCP
- [ ] Paid $0.01 flow confirmed on a live facilitator route (B402 partner application / b402.ai in USDT / self-hosted) — none confirmed as of 2026-11-20; see §6.1
- [ ] At least 30 distinct Agent ID holders have used it (`checkerCount()`) — promotion plan in §14
- [ ] Submitted before 2026-12-09 08:00 UTC
- [ ] Nothing spent beyond deploy gas without Tosin's say-so

## 9. Open decisions for Tosin

1. **Deploy?** With the §5 quote — yes/no, and when.
2. **Free-check allowance per wallet** (draft idea: 5 free deep checks per wallet, enforced off-chain by the backend; the contract does not enforce a cap, so a backend bug cannot lock a holder out on-chain). The free *on-chain* check (`checkMint`) is unlimited — the allowance only applies to the paid deep-check API's free tier.
3. **Who pays `checkMint` gas** — the checker (default: each holder pays their own ~61k–302k gas), or a backend relayer from Tosin's BNB (costs him gas per check; not recommended while his balance is 0.00137 BNB).
4. **Paid-flow route to pursue first:** Binance B402 testnet + partner application, b402.ai mainnet, or self-hosted facilitator? (Recommended: B402 testnet + application in parallel, per §6.) **Update 2026-11-20:** B402 testnet cannot be tested without partner credentials at all (no public base URL — see §6.1), so the application is the gating step for that route, not a parallel one. b402.ai is reachable but does not support U (USDT instead) and its `/verify` timed out on probe. The backend is built and works in test mode regardless of route (§6.1).

## 10. How to reproduce

```bash
cd ~/workspace/goals/agent-id-hackathon-build/mintbeacon
forge build            # solc 0.8.24, clean (one informational lint)
forge test             # 14 tests, no fork, no mainnet state
# live anvil run (plain, no fork):
anvil --port 8545 --chain-id 56
bash test/run_plain_tests.sh        # deploys + exercises every branch
# real-contract verdicts (eth_call simulation vs live BSC):
python3 test/simulate_real_contracts.py
```

## 11. Sources

- Agent ID registry (Sourcify v2): `https://sourcify.dev/server/v2/contract/56/0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F?fields=sources,metadata,abi`
- Hackathon announcement: https://www.binance.com/en/support/announcement/detail/95f5dbe0ea674a48a739b59e046648bf
- Binance B402 quick start: https://developers.binance.com/docs/onchainpay-x402/quick-start — partner account + API credentials; POST /papi/v2/b402/supported; gas sponsored; start on Sandbox (Testnet)
- B402 testnet/mainnet status: https://ourcryptotalk.com/news/binance-x402-bnb-chain-ai-agent-payments — live on BSC Testnet, mainnet access on request
- b402.ai FAQ: https://docs.b402.ai/evm/reference/faq — separate protocol, live on BNB Chain + Base mainnet, USDT/USDC/USD1 on BNB
- x402 seller quickstart + Bazaar: https://docs.x402.org/getting-started/quickstart-for-sellers and https://docs.rail402.dev/bazaar/cataloging — Bazaar catalogues on settled payment; default facilitator testnet-only
- Self-hosted facilitator gotchas: https://dev.to/linknpark/elf-hosting-an-x402-facilitator-the-eip-3009-gotchas-that-almost-cost-us-money-443k and https://docs.xdcai.tech/reference/build-facilitator
- U stablecoin EIP-3009: https://cointelegraph.com/press-releases/u-stablecoin-launches-on-bnb-shain-and-ethereum-by-united-stables
- Superseded draft (MintCheck, old name, gate unverified, uncompiled — do NOT present as the deliverable): `~/workspace/goals/agent-id-hackathon-build/files/mintcheck-draft.md`. Carried over from it: the paid-route research (§6), the free-check allowance idea (§9.2), the who-pays-gas question (§9.3), and the submission checklist shape (§8).

## 12. Design change recommended before deployment

**None blocking.** The contract is deployable as-is. Two things to keep in mind, both already handled in the code/docs: (1) treat LIKELY_FREE as "probably free, deep-check recommended" in the frontend — never present it as a guarantee; (2) if the paid deep-check later needs to *record* its verdict on-chain for holders who cannot run `checkMint` themselves, add a backend-signed `recordCheck(target, verdict, signature)` in a v1.1 — do not weaken the v1 gate to allow it. The v1 contract should ship unchanged.

## 13. Deployment blocker (2026-11-20)

**Deployment via the Binance Agentic Wallet CLI is NOT possible.** Verified
2026-11-20: `baw contract-call preview` requires a `--to` address, and the
CLI has no contract-creation path at all (latest `baw` is 1.12.0, no newer
version). There is **no deployed MintBeacon address yet** — every part of
the project (frontend, MCP, backend, Bazaar draft) treats the address as a
config value, and the paid lane is not live until a payment route is
confirmed working AND the contract is deployed.

**Update 2026-11-20 ~08:40 WAT — deploy_data.txt had a doubled `0x` prefix (`0x0x60c0...`), which would have made a `cast send --create` fail. Fixed and re-verified: the file is now exactly the compiled creation bytecode + the correct constructor args (registry + Tosin's wallet as owner). A ready-to-run deploy script is now in the repo: `script/deploy.sh` — it checks the prefix, prints a fresh gas quote and the deployer balance, asks for confirmation, then deploys with a 1.8M gas cap. It takes the deployer key from `PRIVATE_KEY` env only (never stored in the repo) and spends nothing until confirmed. Syntax-checked (`bash -n` clean) and fail-safe tested (exits cleanly with no key set).**

**Alternatives (no spend until Tosin decides):**

1. **Deploy with forge/cast from any funded wallet** using the repo's
   `deploy_data.txt` (regenerate after any contract change — it is built
   from `out/MintBeacon.sol/MintBeacon.json`). Constructor args: registry
   `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`, owner Tosin's wallet
   `0xF59BC28F2c942a69417652D9359602A755ECCC99`. The deployer pays
   ~0.00013 BNB gas (measured 1,672,694 gas; re-quote fresh at deploy
   time) and **need not be the owner** — the owner is set by the
   constructor arg, so a helper wallet can deploy and Tosin still owns
   it. Tosin's own wallet balance (0.00136812 BNB) covers the deploy
   gas with margin, if he deploys from it directly.
2. **Wait for Binance to add deployment support** to the Agentic Wallet
   CLI — no timeline known; do not count on it before the 2026-12-09
   submission deadline.

## 14. Promotion plan — reaching 30 Agent ID holders

**Goal:** 30 distinct Agent ID holders use the live contract
(`checkerCount() == 30`) before **2026-12-09 08:00 UTC**. The counter only
ever grows and is readable on-chain, so progress is public and honest.
**Realistic note: 30 holders is not guaranteed.** It needs real people to
try the tool, and MintBeacon is new with no existing audience. The plan
below maximises the chance; it does not promise the number.

**The hook is the free check.** Nobody has to pay anything to count:
`probeMint` is a free view call and `checkMint` costs only gas
(61k–302k, ~0.000005–0.000024 BNB). Every post leads with "free" and
with a concrete, checkable example — a live free mint the reader can
verify themselves (e.g. AgentEcho, Mossminds, AgentGate Badge), not a
generic "try my tool".

**Steps, in order:**

1. **Deploy + source-verify first.** Nothing promotes until the contract
   address and BscScan link exist. The frontend link and the MCP tool
   must work before the first post — a dead link on day one wastes the
   launch.
2. **Seed it with real verdicts.** Before posting, run `checkMint` on
   5–10 known contracts (the free ones Tosin already holds plus 1–2 paid
   ones) so the first visitors see recorded verdicts on-chain, not an
   empty contract. Post one screenshot of a verdict as proof it works.
3. **X launch post** (from Tosin's account, once live): what MintBeacon
   does, the free-check link, one example verdict, hashtags
   `#BinanceWalletHackathon` and `#AgenticWallet`, and a line that it is
   built for the Agent ID Hackathon. Pin it. This post also enters the
   **X-post lane: 20 winners share 2,000 USDC (100 USDC each)** for posts
   about a participating project — the post must be about MintBeacon
   (a participating project), not a generic hackathon post.
4. **Post where free-mint lists already circulate.** Tosin already moves
   in the Agent ID / free-mint circles on X — reply to the free-claim
   list posts with a MintBeacon verdict for the contract being shared
   ("checked this one: LIKELY_FREE, 614/20,000 left — verify it here"),
   rather than only posting standalone tweets. A verdict on the exact
   mint people are discussing is worth more than an announcement. Same
   in the Agent ID spaces / group chats he is in: share one checked
   mint per day, with the link.
5. **Daily check-in cadence (until 9 Dec):** one short post a day — a
   new mint checked, the running `checkerCount()`, or a paid mint caught
   ("this 'free' list item is actually 0.001 BNB — MintBeacon flagged
   it PAID"). Keep each post to one verdict + the link. Stop if it
   starts reading as spam; the audience is small and notices.
6. **Ask the holders Tosin already knows.** He has minted alongside
   other Agent ID holders for two weeks; a direct ask ("try one free
   check, it costs only gas") to a handful of them is the fastest
   credible path to the first 10. Do not pay anyone to check — the
   hackathon counts real usage, and paying for it risks the submission.
7. **Track and report honestly.** Read `checkerCount()` (and
   `totalChecks()`) on-chain daily and keep a running tally in the build
   note. If the count stalls below 30 with a few days left, say so
   plainly and submit with what there is — a live, working, honestly
   documented tool — rather than inflating the number.

**What counts:** a distinct Agent ID holder calling `checkMint` (recorded
on-chain). Free `probeMint` views do not count toward `checkerCount()` —
only recorded checks do — so every call to action must ask for a
`checkMint`, not just "try the probe".

## 15. Deployment (2026-11-25) — DEPLOYED ✅

- **Contract:** `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B` (BSC mainnet, chain 56)
- **Deployer:** Tosin's EOA `osinchukwu.bnb` (`0x8b241be57c75f160a5a675773a6f762fd1896929`) via Remix + MetaMask.
- **Deploy gas:** osinchukwu.bnb held 0.0004 BNB (two 0.0002 BNB top-ups from the Agentic Wallet, 2026-11-20 and 2026-11-25); deploy spent ~0.00015346 BNB, leaving ~0.00024654 BNB in the deploy wallet. (The first 0.0002 looked short only because Remix/MetaMask estimated a higher gas price; network gas was 0.05 gwei at the time.)
- **Constructor args (verified on-chain):** registry `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`, owner `0xF59BC28F2c942a69417652D9359602A755ECCC99` (Tosin's Agentic Wallet / tosinchukwu.ai).
- **Verification of the deployment (live RPC, 2026-11-25 ~08:45 WAT):** code present (13,964 bytes runtime); `owner()` = Agentic Wallet; `registry()` = Agent ID registry; `paused()` = false; `distinctCheckers()` = 0; `totalChecks()` = 0 (fresh, unused). `probeMint(AgentEcho, Tosin)` = LIKELY_FREE / REVERT_GATE, minted 804 / 20,000 — matches the pre-deploy simulation exactly.
- **Bytecode match:** the deployed runtime code matches a local compile of the same source with **solc 0.8.24, optimizer OFF, EVM cancun** — identical except for the two immutable slots (owner + registry baked in) and the metadata hash. ⚠️ The Remix deploy was done with the optimizer OFF, not ON/200 runs as originally instructed — the deploy still works and costs a little more gas per call, but **BscScan source verification MUST use optimizer OFF** (see §16), or verification will fail.
- **Creation tx:** pending confirmation from BscScan (BscScan text fetch returned 403 from this VM; a live-browser read of the contract page was requested to capture creator + creation tx hash).
- **Wiring done 2026-11-25:** `MINTBEACON_ADDRESS` set to the deployed address in `frontend/index.html`, as the default in `mcp/server.py` and `backend/server.py`, and in `bazaar/listing.json` (still DRAFT — paid lane not live).
- **Backend gap closed 2026-11-25:** `GET /verify` previously used only the off-chain probe and returned UNKNOWN for AgentEcho while the MCP server (on-chain) returned LIKELY_FREE. Added `Prober.probe_onchain()` (`backend/probe.py`) calling `probeMint` on the deployed contract; `/verify` now tries on-chain first and falls back to the off-chain probe. Re-tested live 2026-11-25: AgentEcho → LIKELY_FREE/REVERT_GATE 804/20,000 (on-chain); Glitchronin → LIKELY_FREE/REVERT_GATE 1,524/5,314 (on-chain). MCP `verify_mint` re-tested live the same way with the same results, source "deployed MintBeacon contract".

## 16. BscScan source verification — settings (use exactly these)

- Contract address: `0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B`
- Compiler: **0.8.24**, language Solidity, **optimizer: OFF (disabled)** — the deployed bytecode is the optimizer-OFF build (13,964 bytes runtime). Do NOT use optimizer ON / 200 runs.
- EVM version: cancun. License: MIT. Constructor args (ABI-encoded, in order): registry `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`, owner `0xF59BC28F2c942a69417652D9359602A755ECCC99`.
- Source: the single file `contracts/MintBeacon.sol` (self-contained, no imports). Verify at https://bscscan.com/address/0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B → Contract tab → "Verify and Publish". **Status: ✅ VERIFIED 2026-11-25** — Remix verified it on BscScan (confirmed by Tosin), and Sourcify independently reports `exact_match` for both creation and runtime bytecode, verifiedAt 2026-11-25T07:35:34Z.
