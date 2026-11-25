#!/usr/bin/env python3
"""Simulate MintBeacon._classify() against REAL BSC contracts via eth_call.

Replicates the contract's decision tree exactly (same selectors, same order),
calling each target directly with from=<MintBeacon address> so msg.sender
matches what the nested staticcall would see. ONE caveat: in eth_call,
tx.origin == from, while in the real nested call tx.origin is the checker
(Tosin, a holder EOA) and msg.sender is MintBeacon. So:
  - AgentEcho: simulation reaches NoAgentId(); the real probe would hit
    OnlyWallets() first. Same verdict/method (LIKELY_FREE / REVERT_GATE).
  - Mossminds freeMint(): simulation reaches "needs.ai Agent ID"; the real
    probe would hit "eoa only" first. Same verdict/method.
For the 5 real targets every probe reverts WITH data, so no gas measurement
is needed; eth_estimateGas is used only as a fallback for empty reverts.
"""
import json, time, urllib.request

HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 MintBeacon-sim/1.0"}
RPCS = ["https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed1.bnbchain.org",
        "https://bsc-dataseed2.bnbchain.org", "https://rpc-bsc.48.club"]
MB = "0xb77a76C5b8CDcF9cf7c1A60D914a144451D73A69"   # MintBeacon (fork) address, used as msg.sender
TOSIN = "0xF59BC28F2c942a69417652D9359602A755ECCC99"

class RpcError(Exception):
    def __init__(self, err):
        self.err = err
        super().__init__(str(err))

def rpc(method, params, tries=10):
    """Returns the result. JSON-RPC error responses raise RpcError immediately
    (a revert is a definitive answer, not a retryable failure); only transport
    failures (timeout / disconnect / bad HTTP) are retried across endpoints."""
    last = None
    for i in range(tries):
        url = RPCS[i % len(RPCS)]
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(url, data=body, headers=HDRS)
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.load(r)
                if "result" in j:
                    return j["result"]
                raise RpcError(j.get("error"))
        except RpcError:
            raise
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError(f"{method} failed after {tries} transport tries: {last}")

def enc(sel, *args):
    out = sel[2:]
    for a in args:
        if isinstance(a, str) and a.startswith("0x"):
            out += a[2:].lower().rjust(64, "0")
        else:
            out += f"{int(a):064x}"
    return "0x" + out

def call(to, data, frm=MB, gas=500000):
    return rpc("eth_call", [{"from": frm, "to": to, "data": data, "value": "0x0",
                             "gas": hex(gas)}, "latest"])

def try_uint(to, data):
    try:
        r = call(to, data)
        if isinstance(r, str) and r.startswith("0x") and len(r) == 66:
            return True, int(r, 16)
    except Exception:
        pass
    return False, 0

def try_bool(to, data):
    ok, v = try_uint(to, data)
    return ok, bool(v)

# selectors
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
GATE_SEL = {"0x91d01199","0xf947691f","0xff592cec","0x7e838e60","0xd43e8b1d",
            "0x0d9ab13f","0x84a7417d","0x73620441","0x9f8129d1","0xba092d16","0xd26e878f"}
PAY_SEL = {"0xcd1c8867","0x11011294","0x8b87dfbb","0x356680b7","0x583aa026",
           "0x43a8b010","0x3c6b4b28","0x0494526a","0x98d4901c","0xe2f844a0","0xf4d678b8"}
AVAIL_SEL = {"0x589ed34b","0x52df9fe5","0x2058b6db","0x5426a580","0x951b974f",
             "0x8522f177","0xd05cb609","0xf6e85041","0xd7d248ba","0x9e87fac8"}
AVAIL_KW = ["sold","closed","limit","claimed","gone","ended","paused","not open","disabled","max"]
PAY_KW = ["bnb","payment","price","insufficient","fee","cost","ether","fund"]
GATE_KW = ["agent","eoa","holder","identity",".ai"]
ERR_STR, PANIC = "0x08c379a0", "0x4e487b71"

def decode_reason(ret):
    try:
        b = bytes.fromhex(ret[2:])
        ln = int.from_bytes(b[36:68], "big")
        return b[68:68+ln].decode("utf-8", "replace")
    except Exception:
        return ""

def classify_revert(ret):
    """Returns (verdict, method) mirroring _classifyRevert. ret is hex or None."""
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

def raw_call(to, data):
    """eth_call returning (success, returndata-hex-or-empty). The revert data
    lives in the JSON-RPC error's `data` field — take it verbatim; only fall
    back to message regexes when the endpoint does not provide it."""
    import re
    try:
        r = rpc("eth_call", [{"from": MB, "to": to, "data": data, "value": "0x0",
                              "gas": hex(500000)}, "latest"])
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
            return False, "0x08c379a0" + (32).to_bytes(32, "big").hex() + len(rb).to_bytes(32, "big").hex() + rb.hex()
        m = re.search(r"0x[0-9a-fA-F]{8,}", msg)
        if m:
            return False, m.group(0)
        return False, ""
    except RuntimeError:
        return False, ""

def classify(target, checker=TOSIN):
    log = []
    code = rpc("eth_getCode", [target, "latest"])
    if code in ("0x", ""):
        return ("UNKNOWN", "NONE", 0, 0, log + ["no code"])
    ok, minted = try_uint(target, enc(S["totalSupply"]))
    if not ok:
        ok, minted = try_uint(target, enc(S["totalMinted"]))
    ok, mx = try_uint(target, enc(S["maxSupply"]))
    if not ok:
        ok, mx = try_uint(target, enc(S["MAX_SUPPLY"]))
    log.append(f"supply minted={minted} max={mx}")
    ok, open_ = try_bool(target, enc(S["mintOpen"]))
    if ok and not open_:
        return ("UNKNOWN", "UNAVAILABLE", minted, mx, log + ["mintOpen=false"])
    # free lane
    ok, cfm = try_bool(target, enc(S["canFreeMint"], checker))
    if ok and cfm:
        return ("FREE", "FREE_LANE_GETTER", minted, mx, log + ["canFreeMint(checker)=true"])
    for name in ("freeLeft", "freeRemainingOf", "freeFor"):
        ok, f = try_uint(target, enc(S[name], checker))
        if ok and f > 0:
            return ("FREE", "FREE_LANE_GETTER", minted, mx, log + [f"{name}(checker)={f}"])
    # prices
    ok, pf = try_uint(target, enc(S["priceFor"], checker))
    if ok and pf == 0:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["priceFor(checker)=0"])
    ok, pfq = try_uint(target, enc(S["priceForQty"], checker, 1))
    if ok and pfq == 0:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["priceFor(checker,1)=0"])
    ok, qf = try_uint(target, enc(S["quoteFor"], checker, 1))
    if ok and qf == 0:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["quote(checker,1)=0"])
    answered = zero = 0; paid = 0
    for name in ("MINT_PRICE", "PRICE", "price", "currentPrice", "basePrice"):
        ok, p = try_uint(target, enc(S[name]))
        if ok:
            answered += 1
            if p == 0: zero += 1
            elif paid == 0: paid = p
            log.append(f"{name}={p}")
    ok, q = try_uint(target, enc(S["quote"], 1))
    if ok:
        answered += 1
        if q == 0: zero += 1
        elif paid == 0: paid = q
        log.append(f"quote(1)={q}")
    if answered > 0 and zero == answered:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["all price getters 0"])
    # probes
    seen = "NONE"
    for name, data in (("freeMint", enc(S["freeMint"])), ("mint", enc(S["mint"])),
                       ("mint(1)", enc(S["mintQty"], 1)), ("claim", enc(S["claim"]))):
        ok, ret = raw_call(target, data)
        if ok:
            log.append(f"{name}: SUCCESS (no state change in static context) -> ignore")
            continue
        if ret in ("", "0x"):
            # empty revert: estimate gas as the floor check proxy
            try:
                g = int(rpc("eth_estimateGas", [{"from": MB, "to": target, "data": data,
                                                  "value": "0x0"}, "latest"]), 16)
            except Exception:
                g = -1
            log.append(f"{name}: EMPTY revert, est gas={g}")
            if g >= 1000:
                return ("LIKELY_FREE", "STATIC_WRITE", minted, mx, log + [f"{name} empty revert, gas>={1000}"])
            continue
        v, m = classify_revert(ret)
        log.append(f"{name}: revert {ret[:74]}... -> {v}/{m}" + (f" reason={decode_reason(ret)!r}" if ret[:10].lower() == ERR_STR else ""))
        if v in ("PAID", "LIKELY_FREE"):
            return (v, m, minted, mx, log)
        if v == "UNKNOWN" and m == "UNAVAILABLE":
            return (v, m, minted, mx, log)
        if m != "NONE":
            seen = m
    if paid > 0:
        return ("PAID", "PRICE_POSITIVE", minted, mx, log + [f"paidPrice={paid}"])
    if seen != "NONE":
        return ("UNKNOWN", seen, minted, mx, log)
    return ("UNKNOWN", "NONE", minted, mx, log)

TARGETS = {
    "AgentEcho (free, mint())": "0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2",
    "Mossminds (free, freeMint())": "0x5b49b06a2e25fce0bca865be70f3c62b6319367a",
    "AgentGate Badge (free, mint())": "0x42c9ed912025df33ca1676aad4651cdded3f09ed",
    "GhostyPepe (PAID 0.001 BNB)": "0x979A49C2fBFa9DEd4bC308de2f54C0E99412A0A4",
    "AgentPunk (PAID 0.0015 BNB)": "0x51Cfae31cda7D4AD1367473792180E22E13D14FF",
}
for name, addr in TARGETS.items():
    print(f"\n=== {name} {addr} ===")
    try:
        v, m, minted, mx, log = classify(addr)
        print(f"VERDICT: {v} / {m} | minted={minted} maxSupply={mx}")
        for line in log:
            print(f"  {line}")
    except Exception as e:
        print(f"SIMULATION FAILED: {e}")
