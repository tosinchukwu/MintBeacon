#!/usr/bin/env python3
"""Off-chain probe mirroring MintBeacon._classify() (contracts/MintBeacon.sol).

Same selectors, same order, same verdict/method vocabulary:
  verdicts: UNKNOWN, FREE, LIKELY_FREE, PAID
  methods:  NONE, FREE_LANE_GETTER, PRICE_ZERO, STATIC_WRITE, REVERT_GATE,
            REVERT_PAYMENT, PRICE_POSITIVE, REVERT_UNKNOWN, UNAVAILABLE

Differences from the on-chain probe (documented, not hidden):
  * eth_call runs with `from` = the checker, so tx.origin == checker. The
    real nested probe runs with msg.sender = MintBeacon and tx.origin =
    checker, so a gate that checks tx.origin (e.g. OnlyWallets) fires at a
    different point. Verdict/method are the same in the cases tested
    (see BUILD_NOTE.md §4b).
  * eth_call cannot measure gas used by an empty revert, so eth_estimateGas
    is used as the floor-check proxy (gas >= 1000 -> STATIC_WRITE), exactly
    as in test/simulate_real_contracts.py.
  * This module never signs, never sends a transaction, never spends
    anything. It only reads via eth_call / eth_getCode / eth_estimateGas.

Stdlib only (urllib). No pip dependencies.
"""
import json
import re
import time
import urllib.request

HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 MintBeacon-backend/1.0"}
DEFAULT_RPCS = [
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-dataseed2.bnbchain.org",
    "https://rpc-bsc.48.club",
]

# Selectors (must match contracts/MintBeacon.sol exactly).
S = dict(
    totalSupply="0x18160ddd", totalMinted="0xa2309ff8", maxSupply="0xd5abeb01",
    MAX_SUPPLY="0x32cb6b0c", mintOpen="0x24bbd049",
    canFreeMint="0xd5b78380", freeLeft="0xeb28217e", freeRemainingOf="0xceda0c40",
    freeFor="0xbde5ccc9", priceFor="0xf12a7add", priceForQty="0xd527979a",
    quoteFor="0x8f79306e", MINT_PRICE="0xc002d23d", PRICE="0x8d859f3e",
    price="0xa035b1fe", currentPrice="0x9d1b464a", basePrice="0xc7876ea4",
    quote="0xed1bd76c", freeMint="0x5b70ea9f", mint="0x1249c58b",
    mintQty="0xa0712d68", claim="0x4e71d92d",
)
GATE_SEL = {"0x91d01199", "0xf947691f", "0xff592cec", "0x7e838e60", "0xd43e8b1d",
            "0x0d9ab13f", "0x84a7417d", "0x73620441", "0x9f8129d1", "0xba092d16", "0xd26e878f"}
PAY_SEL = {"0xcd1c8867", "0x11011294", "0x8b87dfbb", "0x356680b7", "0x583aa026",
           "0x43a8b010", "0x3c6b4b28", "0x0494526a", "0x98d4901c", "0xe2f844a0", "0xf4d678b8"}
AVAIL_SEL = {"0x589ed34b", "0x52df9fe5", "0x2058b6db", "0x5426a580", "0x951b974f",
             "0x8522f177", "0xd05cb609", "0xf6e85041", "0xd7d248ba", "0x9e87fac8"}
AVAIL_KW = ["sold", "closed", "limit", "claimed", "gone", "ended", "paused",
            "not open", "disabled", "max"]
PAY_KW = ["bnb", "payment", "price", "insufficient", "fee", "cost", "ether", "fund"]
GATE_KW = ["agent", "eoa", "holder", "identity", ".ai"]
ERR_STR, PANIC = "0x08c379a0", "0x4e487b71"
WRITE_GAS_FLOOR = 1000  # matches the contract constant

VERDICTS = ("UNKNOWN", "FREE", "LIKELY_FREE", "PAID")
# Method enum order verified against contracts/MintBeacon.sol — do not reorder.
METHODS = ("NONE", "FREE_LANE_GETTER", "PRICE_ZERO", "STATIC_WRITE", "REVERT_GATE",
           "REVERT_PAYMENT", "PRICE_POSITIVE", "REVERT_UNKNOWN", "UNAVAILABLE")


class RpcError(Exception):
    def __init__(self, err):
        self.err = err
        super().__init__(str(err))


class RpcClient:
    """Minimal BSC JSON-RPC client with endpoint rotation on transport errors."""

    def __init__(self, rpcs=None):
        self.rpcs = list(rpcs or DEFAULT_RPCS)

    def call(self, method, params, tries=10):
        last = None
        for i in range(tries):
            url = self.rpcs[i % len(self.rpcs)]
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params}).encode()
                req = urllib.request.Request(url, data=body, headers=HDRS)
                with urllib.request.urlopen(req, timeout=20) as r:
                    j = json.load(r)
                    if "result" in j:
                        return j["result"]
                    raise RpcError(j.get("error"))
            except RpcError:
                raise
            except Exception as e:  # transport failure: rotate endpoint
                last = e
                time.sleep(1)
        raise RuntimeError(f"{method} failed after {tries} transport tries: {last}")

    def eth_call(self, to, data, frm, gas=500000):
        return self.call("eth_call", [{"from": frm, "to": to, "data": data,
                                       "value": "0x0", "gas": hex(gas)}, "latest"])

    def eth_get_code(self, addr):
        return self.call("eth_getCode", [addr, "latest"])

    def eth_get_tx_receipt(self, tx_hash):
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def eth_estimate_gas(self, to, data, frm):
        return self.call("eth_estimateGas", [{"from": frm, "to": to, "data": data,
                                              "value": "0x0"}, "latest"])


def enc(sel, *args):
    out = sel[2:]
    for a in args:
        if isinstance(a, str) and a.startswith("0x"):
            out += a[2:].lower().rjust(64, "0")
        else:
            out += f"{int(a):064x}"
    return "0x" + out


def decode_reason(ret):
    try:
        b = bytes.fromhex(ret[2:])
        ln = int.from_bytes(b[36:68], "big")
        return b[68:68 + ln].decode("utf-8", "replace")
    except Exception:
        return ""


def classify_revert(ret):
    """Mirror _classifyRevert. Returns (verdict, method)."""
    if ret is None or len(ret) < 10:
        return ("UNKNOWN", "REVERT_UNKNOWN")
    sel = ret[:10].lower()
    if sel == ERR_STR:
        reason = decode_reason(ret).lower()
        if any(k in reason for k in AVAIL_KW):
            return ("UNKNOWN", "UNAVAILABLE")
        if any(k in reason for k in PAY_KW):
            return ("PAID", "REVERT_PAYMENT")
        if any(k in reason for k in GATE_KW):
            return ("LIKELY_FREE", "REVERT_GATE")
        return ("UNKNOWN", "REVERT_UNKNOWN")
    if sel == PANIC:
        return ("UNKNOWN", "REVERT_UNKNOWN")
    if sel in GATE_SEL:
        return ("LIKELY_FREE", "REVERT_GATE")
    if sel in PAY_SEL:
        return ("PAID", "REVERT_PAYMENT")
    if sel in AVAIL_SEL:
        return ("UNKNOWN", "UNAVAILABLE")
    return ("UNKNOWN", "REVERT_UNKNOWN")


class Prober:
    def __init__(self, rpc=None):
        self.rpc = rpc or RpcClient()

    def try_uint(self, to, data, frm):
        try:
            r = self.rpc.eth_call(to, data, frm)
            if isinstance(r, str) and r.startswith("0x") and len(r) == 66:
                return True, int(r, 16)
        except Exception:
            pass
        return False, 0

    def try_bool(self, to, data, frm):
        ok, v = self.try_uint(to, data, frm)
        return ok, bool(v)

    def raw_call(self, to, data, frm):
        """eth_call returning (success, returndata-hex-or-empty)."""
        try:
            r = self.rpc.eth_call(to, data, frm)
            return True, r
        except RpcError as e:
            err = e.err
            d = None
            if isinstance(err, dict):
                d = err.get("data")
                if isinstance(d, dict):
                    d = d.get("data") or d.get("result")
            if isinstance(d, str) and d.startswith("0x"):
                m = re.match(r"0x[0-9a-fA-F]*", d)
                if m:
                    return False, m.group(0)
            msg = str(err)
            m = re.search(r"reason string '([^']*)'", msg)
            if m:
                rb = m.group(1).encode()
                return False, ("0x08c379a0" + (32).to_bytes(32, "big").hex()
                               + len(rb).to_bytes(32, "big").hex() + rb.hex())
            m = re.search(r"0x[0-9a-fA-F]{8,}", msg)
            if m:
                return False, m.group(0)
            return False, ""
        except RuntimeError:
            return False, ""

    def classify(self, target, checker):
        """Full probe. Returns dict with verdict, method, minted, maxSupply,
        paidPrice (first positive price seen, 0 if none) and an evidence log."""
        log = []
        code = self.rpc.eth_get_code(target)
        if code in ("0x", "", None):
            return {"verdict": "UNKNOWN", "method": "NONE", "minted": 0,
                    "maxSupply": 0, "paidPrice": 0,
                    "evidence": ["target has no deployed code"]}
        ok, minted = self.try_uint(target, enc(S["totalSupply"]), checker)
        if not ok:
            ok, minted = self.try_uint(target, enc(S["totalMinted"]), checker)
        ok, mx = self.try_uint(target, enc(S["maxSupply"]), checker)
        if not ok:
            ok, mx = self.try_uint(target, enc(S["MAX_SUPPLY"]), checker)
        log.append(f"supply minted={minted} maxSupply={mx}")
        ok, open_ = self.try_bool(target, enc(S["mintOpen"]), checker)
        if ok and not open_:
            return {"verdict": "UNKNOWN", "method": "UNAVAILABLE", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["mintOpen=false"]}
        # Free-lane getters.
        ok, cfm = self.try_bool(target, enc(S["canFreeMint"], checker), checker)
        if ok and cfm:
            return {"verdict": "FREE", "method": "FREE_LANE_GETTER", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["canFreeMint(checker)=true"]}
        for name in ("freeLeft", "freeRemainingOf", "freeFor"):
            ok, f = self.try_uint(target, enc(S[name], checker), checker)
            if ok and f > 0:
                return {"verdict": "FREE", "method": "FREE_LANE_GETTER",
                        "minted": minted, "maxSupply": mx, "paidPrice": 0,
                        "evidence": log + [f"{name}(checker)={f}"]}
        # Checker-priced quotes.
        ok, pf = self.try_uint(target, enc(S["priceFor"], checker), checker)
        if ok and pf == 0:
            return {"verdict": "FREE", "method": "PRICE_ZERO", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["priceFor(checker)=0"]}
        ok, pfq = self.try_uint(target, enc(S["priceForQty"], checker, 1), checker)
        if ok and pfq == 0:
            return {"verdict": "FREE", "method": "PRICE_ZERO", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["priceFor(checker,1)=0"]}
        ok, qf = self.try_uint(target, enc(S["quoteFor"], checker, 1), checker)
        if ok and qf == 0:
            return {"verdict": "FREE", "method": "PRICE_ZERO", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["quote(checker,1)=0"]}
        # Global price getters.
        answered = zero = 0
        paid = 0
        for name in ("MINT_PRICE", "PRICE", "price", "currentPrice", "basePrice"):
            ok, p = self.try_uint(target, enc(S[name]), checker)
            if ok:
                answered += 1
                if p == 0:
                    zero += 1
                elif paid == 0:
                    paid = p
                log.append(f"{name}={p}")
        ok, q = self.try_uint(target, enc(S["quote"], 1), checker)
        if ok:
            answered += 1
            if q == 0:
                zero += 1
            elif paid == 0:
                paid = q
            log.append(f"quote(1)={q}")
        if answered > 0 and zero == answered:
            return {"verdict": "FREE", "method": "PRICE_ZERO", "minted": minted,
                    "maxSupply": mx, "paidPrice": 0,
                    "evidence": log + ["all price getters 0"]}
        # Mint-function probes.
        seen = "NONE"
        for name, data in (("freeMint", enc(S["freeMint"])),
                           ("mint", enc(S["mint"])),
                           ("mint(1)", enc(S["mintQty"], 1)),
                           ("claim", enc(S["claim"]))):
            ok, ret = self.raw_call(target, data, checker)
            if ok:
                log.append(f"{name}: SUCCESS (no state change in static context) -> ignore")
                continue
            if ret in ("", "0x"):
                try:
                    g = int(self.rpc.eth_estimate_gas(target, data, checker), 16)
                except Exception:
                    g = -1
                log.append(f"{name}: EMPTY revert, est gas={g}")
                if g >= WRITE_GAS_FLOOR:
                    return {"verdict": "LIKELY_FREE", "method": "STATIC_WRITE",
                            "minted": minted, "maxSupply": mx, "paidPrice": paid,
                            "evidence": log + [f"{name} empty revert, gas>={WRITE_GAS_FLOOR}"]}
                continue
            v, m = classify_revert(ret)
            log.append(f"{name}: revert {ret[:74]}... -> {v}/{m}"
                       + (f" reason={decode_reason(ret)!r}"
                          if ret[:10].lower() == ERR_STR else ""))
            if v in ("PAID", "LIKELY_FREE"):
                return {"verdict": v, "method": m, "minted": minted,
                        "maxSupply": mx, "paidPrice": paid, "evidence": log}
            if v == "UNKNOWN" and m == "UNAVAILABLE":
                return {"verdict": v, "method": m, "minted": minted,
                        "maxSupply": mx, "paidPrice": paid, "evidence": log}
            if m != "NONE":
                seen = m
        if paid > 0:
            return {"verdict": "PAID", "method": "PRICE_POSITIVE", "minted": minted,
                    "maxSupply": mx, "paidPrice": paid,
                    "evidence": log + [f"paidPrice={paid}"]}
        if seen != "NONE":
            return {"verdict": "UNKNOWN", "method": seen, "minted": minted,
                    "maxSupply": mx, "paidPrice": paid, "evidence": log}
        return {"verdict": "UNKNOWN", "method": "NONE", "minted": minted,
                "maxSupply": mx, "paidPrice": paid, "evidence": log}

    def probe_onchain(self, mintbeacon, target, checker):
        """Call probeMint(target, checker) on the DEPLOYED MintBeacon contract
        (view call, no signature). Returns the same dict shape as classify().
        Raises on RPC failure or malformed returndata so the caller can fall
        back to the off-chain classify(). Added 2026-11-25 after deployment."""
        ret = self.rpc.eth_call(mintbeacon, enc("0x0dc26b37", target, checker),
                                checker)
        if not ret or len(ret) < 2 + 64 * 4:
            raise RuntimeError(f"probeMint returned unexpected data: {ret!r}")
        words = [int(ret[2 + 64 * i:2 + 64 * (i + 1)], 16) for i in range(4)]
        verdict_i, method_i, minted, mx = words
        if verdict_i >= len(VERDICTS) or method_i >= len(METHODS):
            raise RuntimeError(f"probeMint out-of-range verdict/method: {words}")
        return {"verdict": VERDICTS[verdict_i], "method": METHODS[method_i],
                "minted": minted, "maxSupply": mx, "paidPrice": 0,
                "evidence": [f"probeMint on deployed MintBeacon {mintbeacon}"],
                "source": "on-chain"}


def sourcify_status(target, timeout=20):
    """Source-verification status from Sourcify v2. Returns a dict with
    `match` (exact_match / partial_match / null) and `verified` bool.
    Never raises: returns {"verified": False, "match": None, "error": ...}."""
    url = (f"https://sourcify.dev/server/v2/contract/56/{target}"
           "?fields=stdJson,creationMatch,match")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MintBeacon-backend/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.load(r)
        match = j.get("match") or j.get("stdJson", {}).get("match")
        if isinstance(match, dict):
            match = match.get("match") or match.get("status")
        return {"verified": match in ("exact_match", "partial_match"),
                "match": match, "source": "sourcify.dev"}
    except Exception as e:
        return {"verified": False, "match": None, "error": str(e),
                "source": "sourcify.dev"}


def risk_flags(probe, source_status, target):
    """Heuristic flags for the deep check. These are observations, not a
    safety guarantee — a clean flag list does not prove a contract is safe."""
    flags = []
    if not source_status.get("verified"):
        flags.append({"code": "SOURCE_UNVERIFIED",
                      "detail": "Target source is not verified on Sourcify; "
                                "its mint logic cannot be read."})
    if probe["method"] in ("NONE", "REVERT_UNKNOWN"):
        flags.append({"code": "NO_READABLE_MINT_PATH",
                      "detail": "No free-lane getter, price getter or mint "
                                "entry point gave a readable answer."})
    if probe["verdict"] == "PAID":
        flags.append({"code": "PAID_LANE_ONLY",
                      "detail": f"Paid lane observed (price={probe.get('paidPrice', 0)} wei); "
                                "no free lane found for this checker."})
    if probe["method"] == "UNAVAILABLE":
        flags.append({"code": "MINT_CLOSED",
                      "detail": "Mint window reports closed / sold out / "
                                "already claimed for this checker."})
    if probe["verdict"] == "LIKELY_FREE" and probe["method"] == "STATIC_WRITE":
        flags.append({"code": "STATIC_WRITE_AMBIGUITY",
                      "detail": "Mint function reverted empty after real gas "
                                "burn; a bare revert or gas-griefing target "
                                "looks identical — treat as probable, not proven."})
    return flags
