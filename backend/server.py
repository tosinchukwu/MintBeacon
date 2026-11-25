#!/usr/bin/env python3
"""MintBeacon backend — free verify tier + paid deep-check API.

Stdlib only (http.server + urllib). No pip dependencies. Run:
    python3 server.py            # listens on 127.0.0.1:8000 by default

Endpoints
---------
GET  /                                  Service status + paid-lane status.
GET  /verify?contract=0x...&checker=0x...
                                     FREE tier. On-chain probeMint via the
                                     deployed MintBeacon contract (falls back
                                     to the off-chain probe in probe.py).
                                     No signature, no payment, no spend.
POST /deep-check  {"contract": "0x...", "payer": "0x..."}
                                     Always answers HTTP 402 with the x402
                                     payment requirements: $0.01 in U
                                     (EIP-3009) to Tosin's wallet.
POST /deep-check/claim  {"contract": "0x...", "payer": "0x...",
                         "txHash": "0x..."}
                                     Presents a settled U payment. The server
                                     verifies the ACTUAL Transfer event
                                     on-chain (U contract, from=payer,
                                     to=payTo, value=$0.01) and only then
                                     runs the deep check: probe simulated
                                     from the payer's own address + Sourcify
                                     source verification + risk flags.
                                     One txHash pays for one deep check
                                     (replay-guarded by a local claim store).

HONEST STATUS — read before using the paid lane
------------------------------------------------
* The MintBeacon contract IS deployed (2026-11-25, BSC mainnet,
  0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B, owner = Tosin's Agentic
  Wallet, registry = Agent ID registry; verified live on-chain — see
  BUILD_NOTE.md, "Deployment (2026-11-25)"). The free /verify tier now
  uses the on-chain probeMint when MINTBEACON_ADDRESS is set (default).
* NO facilitator is wired in. Binance B402 needs a partner account
  (clientId + accessToken + RSA key + IP whitelist) — its base URL is
  not even published ("please contact us for access" for Sandbox AND
  Production), so no testnet call was possible. b402.ai's facilitator
  is reachable for health checks but supports USDT/USDC/USD1 on BSC,
  NOT U, and its /verify endpoint timed out on probe requests.
  The self-hosted facilitator route is unbuilt.
* Therefore the paid lane is NOT LIVE. /deep-check returns a correct
  402 shape, and /deep-check/claim verifies a real on-chain U Transfer
  and returns a real deep-check result — but that is LOCAL VERIFICATION
  ONLY / TEST MODE: there is no facilitator settling payments, no
  Bazaar cataloguing, and no live $0.01 product until one route is
  confirmed working. Do not present the paid lane as live.
* Never trust nonce state alone: the claim step checks the Transfer
  LOG on-chain, not an authorization nonce. A payer can burn their own
  nonce without paying; only a Transfer to payTo counts.

Configuration (environment variables)
--------------------------------------
MINTBEACON_ADDRESS   Deployed MintBeacon contract (empty = not deployed).
MINTBEACON_RPC_URL   Comma-separated BSC RPC URLs.
MINTBEACON_PAY_TO    Recipient of deep-check payments (default Tosin's wallet).
MINTBEACON_HOST      Bind host (default 127.0.0.1).
MINTBEACON_PORT      Bind port (default 8000).
MINTBEACON_CLAIMS_FILE  Claim store path (default backend/data/claims.json).
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from probe import Prober, RpcClient, sourcify_status, risk_flags

# ---------------------------------------------------------------- config
PAY_TO = os.environ.get(
    "MINTBEACON_PAY_TO",
    "0xF59BC28F2c942a69417652D9359602A755ECCC99").lower()
U_ASSET = "0xcE24439F2D9C6a2289F741120FE202248B666666"  # U stablecoin, BSC, 18 decimals
U_ASSET_LOWER = U_ASSET.lower()
PRICE_WEI = 10 ** 16            # $0.01 in U (18 decimals) — verified on-chain 2026-11-20
PRICE_DISPLAY = "$0.01"
NETWORK = "eip155:56"           # BSC mainnet
CHAIN_ID = 56
MINTBEACON_ADDRESS = os.environ.get(
    "MINTBEACON_ADDRESS", "0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B").lower() or None  # deployed 2026-11-25
HOST = os.environ.get("MINTBEACON_HOST", "127.0.0.1")
PORT = int(os.environ.get("MINTBEACON_PORT", "8000"))
CLAIMS_FILE = os.environ.get(
    "MINTBEACON_CLAIMS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "claims.json"))

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
# keccak256("Transfer(address,address,uint256)")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

prober = Prober(RpcClient(
    [u.strip() for u in os.environ.get("MINTBEACON_RPC_URL", "").split(",") if u.strip()]
    or None))
_claims_lock = threading.Lock()


# ------------------------------------------------------------ claim store
def _load_claims():
    try:
        with open(CLAIMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_claims(claims):
    os.makedirs(os.path.dirname(CLAIMS_FILE), exist_ok=True)
    tmp = CLAIMS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(claims, f, indent=2, sort_keys=True)
    os.replace(tmp, CLAIMS_FILE)


# ------------------------------------------------------------ http helpers
def _is_addr(v):
    return isinstance(v, str) and bool(ADDR_RE.match(v))


def _pad_addr(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def _verify_u_transfer(receipt, payer, amount_wei=PRICE_WEI):
    """Check the receipt for a real U Transfer(payer -> PAY_TO, amount).
    Returns (ok, detail). This is the payment check: the Transfer LOG,
    not nonce state, not a signature, not a claim."""
    if not receipt:
        return False, "no receipt found for txHash (unknown or still pending)"
    if receipt.get("status") not in ("0x1", 1):
        return False, f"transaction status is not success: {receipt.get('status')}"
    if str(receipt.get("to", "")).lower() != U_ASSET_LOWER:
        return False, ("transaction was not sent to the U contract "
                       f"({receipt.get('to')}); expected {U_ASSET}")
    for log in receipt.get("logs", []):
        if str(log.get("address", "")).lower() != U_ASSET_LOWER:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if topics[1].lower() != _pad_addr(payer):
            continue
        if topics[2].lower() != _pad_addr(PAY_TO):
            continue
        try:
            value = int(log.get("data", "0x0"), 16)
        except Exception:
            continue
        if value == amount_wei:
            return True, ("U Transfer verified on-chain: "
                          f"{payer} -> {PAY_TO}, {amount_wei} wei U "
                          f"({PRICE_DISPLAY}), tx {receipt.get('transactionHash')}")
        return False, (f"U Transfer to {PAY_TO} found but value is {value} wei, "
                       f"expected {amount_wei} wei ({PRICE_DISPLAY})")
    return False, ("no U Transfer log from payer to payee for the expected "
                   f"amount found in this transaction")


def payment_requirements():
    """The x402 v2 payment requirements for one deep check."""
    return {
        "scheme": "eip3009",
        "network": NETWORK,
        "asset": U_ASSET,
        "amount": str(PRICE_WEI),
        "amountDisplay": PRICE_DISPLAY,
        "payTo": PAY_TO,
        "description": "MintBeacon deep check: source verification + simulation "
                       "from the payer's own address + risk flags for one contract.",
        # NOTE: Binance B402 requires `extra` copied verbatim from its
        # /supported response (name, version, assetTransferMethod,
        # signerAddress, spenderAddress for permit2-*). We have no
        # /supported response — no partner credentials — so `extra` is
        # intentionally absent. A buyer/facilitator that needs it cannot
        # build a valid B402 payload from this 402 alone. That is honest:
        # the facilitator route is not wired yet.
        "extra": None,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout clean
        pass

    def _send(self, status, obj, extra_headers=None):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 65536:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _status(self):
        return {
            "service": "mintbeacon-backend",
            "mintbeaconAddress": MINTBEACON_ADDRESS,
            "mintbeaconDeployed": bool(MINTBEACON_ADDRESS),
            "network": NETWORK,
            "payTo": PAY_TO,
            "deepCheckPrice": {"amountWei": str(PRICE_WEI), "display": PRICE_DISPLAY,
                               "asset": U_ASSET},
            # Honest status: the paid lane is NOT live until a facilitator
            # route is confirmed working AND the contract is deployed.
            "paidLane": {
                "live": False,
                "mode": "test-mode-local-verification-only",
                "facilitator": "none-configured",
                "detail": "POST /deep-check returns a correct 402 shape and "
                          "POST /deep-check/claim verifies a real on-chain U "
                          "Transfer and returns a real deep-check result, but "
                          "no facilitator settles payments, so no live $0.01 "
                          "product exists yet. See backend/README.md § Payment "
                          "routes for what was tested and what is blocked.",
            },
            "endpoints": {
                "GET /verify?contract=0x...&checker=0x...": "free tier probe",
                "POST /deep-check": "402 payment requirements ($0.01 in U)",
                "POST /deep-check/claim": "present a settled U txHash, get the deep check",
            },
        }

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._send(200, self._status())
        if parsed.path == "/verify":
            qs = parse_qs(parsed.query)
            contract = (qs.get("contract") or [""])[0]
            checker = (qs.get("checker") or [PAY_TO])[0]
            if not _is_addr(contract):
                return self._send(400, {"error": "query param contract must be a 0x address"})
            if not _is_addr(checker):
                return self._send(400, {"error": "query param checker must be a 0x address"})
            try:
                source = "off-chain"
                if MINTBEACON_ADDRESS:
                    try:
                        result = prober.probe_onchain(MINTBEACON_ADDRESS,
                                                      contract, checker)
                        source = "on-chain"
                    except Exception:
                        result = prober.classify(contract, checker)
                else:
                    result = prober.classify(contract, checker)
            except Exception as e:
                return self._send(502, {"error": f"probe failed: {e}"})
            return self._send(200, {
                "tier": "free",
                "contract": contract,
                "checker": checker,
                "verdict": result["verdict"],
                "method": result["method"],
                "minted": result["minted"],
                "maxSupply": result["maxSupply"],
                "paidPriceWei": result.get("paidPrice", 0),
                "evidence": result["evidence"],
                "mintbeaconAddress": MINTBEACON_ADDRESS,
                "source": source,
                "note": ("On-chain probeMint via the deployed MintBeacon contract. "
                         if source == "on-chain" else
                         "Off-chain probe mirroring MintBeacon._classify() "
                         "(on-chain probe unavailable, fell back). ")
                        + "LIKELY_FREE means 'probably free, deep-check "
                          "recommended' — never a guarantee. See BUILD_NOTE.md §3.",
            })
        if parsed.path == "/deep-check/claim":
            qs = parse_qs(parsed.query)
            tx_hash = (qs.get("txHash") or [""])[0].lower()
            with _claims_lock:
                claims = _load_claims()
            claim = claims.get(tx_hash)
            if not claim:
                return self._send(404, {"error": "no claim recorded for this txHash"})
            return self._send(200, claim)
        return self._send(404, {"error": "unknown route"})

    # -- POST ----------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/deep-check":
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": "JSON body required"})
            contract = body.get("contract", "")
            payer = body.get("payer", "")
            if not _is_addr(contract):
                return self._send(400, {"error": "field contract must be a 0x address"})
            if payer and not _is_addr(payer):
                return self._send(400, {"error": "field payer must be a 0x address"})
            reqs = payment_requirements()
            payload = {
                "error": "Payment Required",
                "contract": contract,
                "payer": payer or None,
                "paymentRequirements": reqs,
                "paidLaneLive": False,
                "mode": "test-mode-local-verification-only",
                "howToPay": (
                    "1. Send exactly 0.01 U to " + PAY_TO + " on BSC (chain 56) "
                    "from the payer wallet — a plain U transfer. "
                    "2. POST /deep-check/claim with {contract, payer, txHash}. "
                    "The server verifies the actual U Transfer event on-chain "
                    "and only then returns the deep-check result. "
                    "No facilitator is wired yet, so this is local verification "
                    "only — the paid lane is NOT live as a product."),
                "facilitator": "none-configured",
            }
            return self._send(402, payload, extra_headers={
                "X-PAYMENT-REQUIREMENTS": json.dumps(reqs),
            })
        if parsed.path == "/deep-check/claim":
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": "JSON body required"})
            contract = body.get("contract", "")
            payer = body.get("payer", "")
            tx_hash = str(body.get("txHash", "")).lower()
            if not _is_addr(contract):
                return self._send(400, {"error": "field contract must be a 0x address"})
            if not _is_addr(payer):
                return self._send(400, {"error": "field payer must be a 0x address"})
            if not TX_RE.match(tx_hash):
                return self._send(400, {"error": "field txHash must be a 0x transaction hash"})
            with _claims_lock:
                claims = _load_claims()
                if tx_hash in claims:
                    # Replay guard: one settled payment = one deep check.
                    return self._send(200, claims[tx_hash])
            try:
                receipt = prober.rpc.eth_get_tx_receipt(tx_hash)
            except Exception as e:
                return self._send(502, {"error": f"could not fetch receipt: {e}"})
            ok, detail = _verify_u_transfer(receipt, payer)
            if not ok:
                return self._send(402, {
                    "error": "Payment not verified",
                    "detail": detail,
                    "mode": "test-mode-local-verification-only",
                    "paymentRequirements": payment_requirements(),
                })
            # Payment verified on-chain. Now the deep check itself.
            try:
                probe = prober.classify(contract, payer)
            except Exception as e:
                return self._send(502, {"error": f"deep-check probe failed: {e}"})
            source = sourcify_status(contract)
            flags = risk_flags(probe, source, contract)
            result = {
                "tier": "deep-check",
                "mode": "test-mode-local-verification-only",
                "paidLaneLive": False,
                "contract": contract,
                "payer": payer,
                "txHash": tx_hash,
                "payment": {"verified": True, "detail": detail,
                            "amountWei": str(PRICE_WEI), "asset": U_ASSET,
                            "payTo": PAY_TO},
                "verdict": probe["verdict"],
                "method": probe["method"],
                "minted": probe["minted"],
                "maxSupply": probe["maxSupply"],
                "paidPriceWei": probe.get("paidPrice", 0),
                "evidence": probe["evidence"],
                "simulatedFrom": payer,
                "simulationNote": "Probe simulated from the payer's own "
                                  "address, so the Agent ID gate passes for a "
                                  "holder — this is what resolves LIKELY_FREE "
                                  "cases the on-chain probe cannot prove.",
                "sourceVerification": source,
                "riskFlags": flags,
                "note": "Deep check paid by a verified on-chain U Transfer. "
                        "Local verification only — no facilitator settled "
                        "this payment and the paid lane is not live as a "
                        "product until a facilitator route is confirmed.",
            }
            with _claims_lock:
                claims = _load_claims()
                claims[tx_hash] = result
                _save_claims(claims)
            return self._send(200, result)
        return self._send(404, {"error": "unknown route"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MintBeacon backend listening on http://{HOST}:{PORT}")
    print(f"  payTo: {PAY_TO}   price: {PRICE_DISPLAY} in U ({U_ASSET})")
    print(f"  mintbeacon: {MINTBEACON_ADDRESS or 'NOT DEPLOYED (free tier still works)'}")
    print("  paid lane: NOT LIVE — test-mode local verification only (see README)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
