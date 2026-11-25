# MintBeacon — Bazaar listing prep (DRAFT)

`listing.json` is the prepared Bazaar listing for MintBeacon. **Status:
DRAFT — do not submit.** It becomes submittable only once both of these
are true:

1. A paid flow is confirmed working (a facilitator settles a real $0.01
   payment), and
2. The MintBeacon contract is deployed on BSC and source-verified.

As of 2026-11-20 neither is true (see BUILD_NOTE.md §6 test log and
"Deployment blocker (2026-11-20)").

## How Bazaar listing actually works

There is **no separate seller registration**. A resource is catalogued in
the B402 Bazaar automatically when a payment settles carrying the Bazaar
extension (`extensions.bazaar` on the payment). So "submitting" this
listing means: host the backend at a public URL, wire a working
facilitator, and complete one settled paid deep check with the Bazaar
extension set. Then verify it appears in the public discovery API:

```
GET https://www.binance.com/bapi/ramp/v1/public/ramp/b402/bazaar/resources
```

(That endpoint is public, no auth — tested live 2026-11-20: HTTP 200,
1,010 resources. It also confirms U `0xcE24439…666666` is an accepted
B402 asset on BSC mainnet.)

## What is in listing.json

- **name / description / type / x402Version:** MintBeacon, `http`, x402 v2.
- **resource:** placeholder `https://mintbeacon.example/api/deep-check` —
  replace with the real public backend URL once hosted. The free tier is
  `GET /verify?contract=0x...` on the same host.
- **accepts:** one entry — scheme `eip3009`, network `eip155:56`,
  asset U `0xcE24439F2D9C6a2289F741120FE202248B666666` (18 decimals),
  `maxAmountRequired` `10000000000000000` (= $0.01), payTo Tosin's wallet
  `0xF59BC28F2c942a69417652D9359602A755ECCC99`.
- **pricing:** free tier (on-chain probe, unlimited) + paid tier ($0.01
  per deep check in U).
- **contract.address:** `null` until deployed — fill in after deployment
  with the BscScan link.
- **facilitator:** `none-configured` — honest status, not a placeholder
  to forget. See `backend/README.md` § "Payment routes" for what was
  tested per route (Binance B402 blocked on partner onboarding; b402.ai
  live for health but does not support U and its /verify timed out;
  self-hosted unbuilt).

## Checklist before this stops being a draft

- [ ] MintBeacon deployed on BSC + source-verified on BscScan
- [ ] Backend hosted at a public URL (replace `resource`)
- [ ] One facilitator route confirmed working end-to-end (real $0.01 settle)
- [ ] One settled deep-check payment carrying the Bazaar extension
- [ ] Listing visible at the public Bazaar `/resources` endpoint
- [ ] `listing.json`: status flipped from DRAFT, contract address filled in
