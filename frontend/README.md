# MintBeacon frontend

Single-page app for the free tier. Vanilla HTML/CSS/JS — no build step, no
external JS dependencies. The tiny JSON-RPC + ABI encoding is hand-rolled
(`probeMint` calldata is its 4-byte selector + two 32-byte-padded
addresses; returns are decoded as 32-byte words).

## How to open it

Just open `index.html` in a browser, or serve the folder:

```
cd frontend
python3 -m http.server 8000
# then open http://localhost:8000/
```

All four public BSC RPC endpoints used by the page allow CORS
(`Access-Control-Allow-Origin: *`, verified 2026-11-20), so opening the
file directly works too.

## How to set the deployed address

MintBeacon is **not deployed yet** — deployment via the Binance Agentic
Wallet CLI is blocked (`baw contract-call preview` needs a `--to`
address and the CLI has no contract-creation path; verified 2026-11-20,
baw 1.12.0). So the page ships with the contract address unset:

```js
const MINTBEACON_ADDRESS = "";   // top of the <script> in index.html
```

Once deployed, either:

1. edit that line in `index.html`, or
2. paste the deployed address in the "MintBeacon contract address" field
   in the page and click **Use this address** (remembered in that browser
   via `localStorage`).

While no address is set, the page says so clearly — *"MintBeacon is not
deployed yet — the on-chain check button is disabled until the deployed
address is set"* — the on-chain button stays disabled, the
checker/total stats show "—", and the free off-chain probe is still
fully available. No address is ever faked and the page never claims to
be deployed.

## What each part does

1. **Verify (free probe).** Paste a target contract address and a
   checker address (defaults to the owner's wallet,
   `0xF59BC28F2c942a69417652D9359602A755ECCC99`; "Connect wallet" swaps
   in the connected wallet's address). The probe runs read-only
   `eth_call`s only — no signature, no gas — against public BSC RPC
   endpoints with fallback
   (`bsc-dataseed.bnbchain.org`, `bsc-dataseed1/2.bnbchain.org`,
   `rpc.48.club`; transport failures retry on the next endpoint, a
   JSON-RPC error such as a revert is a definitive answer and is not
   retried).
   - **While undeployed:** the probe calls the target contract directly
     (off-chain), running the exact classification from
     `test/simulate_real_contracts.py` — supply reads, mint-window
     check, free-lane getters, price getters, then mint-function
     probes (`freeMint` → `mint` → `mint(uint256)` → `claim`),
     classifying reverts the same way `MintBeacon.sol` does.
   - **Once deployed:** the page calls `probeMint(target, checker)` on
     MintBeacon itself (authoritative) and also reads the recorded
     verdict via `verdictOf(target)`. If the on-chain probe fails it
     falls back to the off-chain probe and says so.
   - The result shows the verdict, a plain-language explanation, how
     it was decided (method), minted/maxSupply, and the Agent ID gate
     status for the checker (`balanceOf` on the registry
     `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F` — balance > 0 means
     that address holds an Agent ID). A collapsible probe trace lists
     every call the probe made.
2. **Check on-chain (optional, deployed only).** Builds a
   `checkMint(target)` transaction and shows a **full preview first**
   (to, function, calldata, value 0 BNB, estimated gas via
   `eth_estimateGas`, gas price, estimated cost in BNB, from). It is
   only sent after the user clicks "Confirm & send in wallet" **and**
   approves in their own wallet (`window.ethereum` / injected wallet
   provider). The page checks the wallet is on BSC mainnet (chain id
   56, offers a switch) and that the connected wallet holds an Agent
   ID — otherwise it warns that `checkMint` would revert and disables
   sending, so no gas is wasted. Never auto-signs, never requests a
   signature without the preview.
3. **Stats.** When deployed, `checkerCount()` (distinct Agent ID
   holders who recorded a check — the 30-holder bar) and `totalChecks()`
   are shown live at the top.

## Honest limits

- **LIKELY_FREE means "probably free — deep-check recommended", never
  a guarantee of free.** The probe saw no payment but could not prove
  the mint costs nothing. A paid mint whose payment check runs after
  its identity gate can look LIKELY_FREE. Only the paid deep-check
  (off-chain source verification + simulation from the checker's own
  address) resolves those cases.
- **Off-chain probe gap (undeployed mode):** a fully open mint can read
  UNKNOWN off-chain, because a plain `eth_call` simulates the mint
  successfully and discards the state change, while the on-chain probe
  would catch the state-change attempt as LIKELY_FREE. The on-chain
  probe is authoritative once deployed. The page states this in the
  footer.
- The probe never mints from the target, never sends it value, and
  never looks up a wallet by Agent ID name. The only identity question
  is "does this address hold an Agent ID?"
- A "free" verdict says nothing about whether the token will ever sell
  or be worth anything. No promised returns.
- No QR-code login or pairing codes anywhere; the only wallet
  interaction is the standard injected-provider connect and the
  user-confirmed `checkMint` send.

## Testing

`test/simulate_real_contracts.py` is the reference implementation of
the classification — the page's JS is a line-for-line port of it
(same selectors, same order, same revert classification). Live-tested
2026-11-20 (see the test harness notes in the build report):

- AgentEcho `0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2` → LIKELY_FREE /
  REVERT_GATE (expected).
- GhostyPepe `0x979A49C2fBFa9DEd4bC308de2f54C0E99412A0A4` → PAID /
  REVERT_PAYMENT (expected).
- AgentGate Badge → LIKELY_FREE / REVERT_GATE; AgentPunk → PAID /
  REVERT_PAYMENT (via the Python reference; same logic as the page).
- The on-chain path (`probeMint` / `checkMint` / stats) cannot be
  tested end-to-end until MintBeacon is deployed — the calldata
  encoding was verified against the ABI selectors instead (see build
  report). The "Check on-chain" button stays disabled until then.

Note: the BSC dataseed endpoints are flaky from some networks (the
reference run hit transient `IncompleteRead` transport failures that
cleared on retry). The page's RPC layer retries across all four
endpoints for exactly that reason; a failed probe is worth one retry.
