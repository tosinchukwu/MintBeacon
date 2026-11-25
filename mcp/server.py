#!/usr/bin/env python3
"""MintBeacon MCP server — free-mint verifier for agents on BNB Smart Chain.

Exposes three tools over MCP (JSON-RPC 2.0). This module is the local stdio
transport; mcp/http_server.py reuses the same request handler for remote
Streamable HTTP:

  * mintbeacon_status — inspect the deployed contract and on-chain counters
  * verify_mint       — classify a contract as FREE / LIKELY_FREE / PAID / UNKNOWN
  * free_mints_feed   — list currently free, live mints from mints-bnb.pages.dev

Standard library only (no pip dependencies), so it runs anywhere Python 3
is installed. It NEVER signs, NEVER sends transactions and NEVER holds keys:
everything here is eth_call / eth_getCode / eth_estimateGas (reads only).

Two operating modes for verify_mint
------------------------------------
1. Deployed contract (preferred): set MINTBEACON_ADDRESS (env) or pass
   --mintbeacon <address>. verify_mint then calls probeMint(target, checker)
   on the deployed MintBeacon contract and reports the on-chain verdict.
2. Off-chain probe (fallback mode):
   when no deployment address is configured, verify_mint runs the same
   classification directly against the target contract (code check, supply reads, free-lane
   getters, price getters, value-0 eth_call probes of mint()/freeMint()/
   mint(uint256)/claim() with revert classification). The logic mirrors
   test/simulate_real_contracts.py and contracts/MintBeacon.sol, and the
   result is labelled clearly as an off-chain probe.

One deliberate adaptation vs the on-chain contract: the contract probes the
target with STATICCALLs from the MintBeacon address, so a mint function that
would succeed reverts with empty data (static context forbids state
changes) and reads as LIKELY_FREE. Off-chain we use plain eth_call, which
simulates the call fully: a mint function that EXECUTES successfully with
value 0 is reported as LIKELY_FREE (never FREE — the probe sender is not
the checker, so it proves freeness for the probe sender, not personally
for the checker; the paid deep-check is the backstop for confirmation).

The off-chain mint probes are sent from PROBE_FROM, a fixed address that
holds no Agent ID — the same role the MintBeacon contract address plays
on-chain (it is the msg.sender the target sees). That is why an Agent
ID-gated mint shows up as LIKELY_FREE / REVERT_GATE here instead of
passing the gate: the probe cannot impersonate the checker. Free-lane and
price getters are always read FOR the checker address, so a personal free
lane still reports FREE.

No name-to-wallet lookup exists anywhere in this server. The only identity
question ever asked is "does THIS checker address hold an Agent ID?" via
the registry's balanceOf — the same check every Agent ID-gated mint
already performs.

Honest limits (same as the contract — see MintBeacon.sol header):
  * The probe can PROVE "paid" but can only SUGGEST "free". A paid mint
    whose payment check runs after its identity gate looks LIKELY_FREE.
  * LIKELY_FREE means "probably free — deep-check recommended", NEVER a
    guarantee. A gas-griefing target can also read as LIKELY_FREE.
  * It cannot see off-chain allowlists, signature-gated mints, or time
    windows that open later.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "0.2.0"
SERVER_NAME = "mintbeacon"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RPCS = [
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-dataseed2.bnbchain.org",
    "https://rpc.48.club",
]
RPC_HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 MintBeacon-mcp/1.0"}
FEED_URL = "https://mints-bnb.pages.dev/projects.json"

REGISTRY = "0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F"  # Agent ID registry (BSC)
DEFAULT_CHECKER = "0xF59BC28F2c942a69417652D9359602A755ECCC99"  # Tosin's wallet

# Sender used for off-chain mint probes. Mirrors test/simulate_real_contracts.py:
# the address the deployed MintBeacon contract would call from — it holds no
# Agent ID, so identity gates surface as REVERT_GATE instead of being passed.
PROBE_FROM = "0xb77a76C5b8CDcF9cf7c1A60D914a144451D73A69"

VERDICTS = ["UNKNOWN", "FREE", "LIKELY_FREE", "PAID"]
VERDICT_MEANING = {
    "UNKNOWN": "could not determine — no conclusive signal",
    "FREE": "free, gas-only mint (positive proof for this checker)",
    "LIKELY_FREE": "probably free — deep-check recommended",
    "PAID": "paid mint — a payment requirement was observed",
}
# Method enum order verified against contracts/MintBeacon.sol — do not reorder.
METHODS = [
    "NONE", "FREE_LANE_GETTER", "PRICE_ZERO", "STATIC_WRITE", "REVERT_GATE",
    "REVERT_PAYMENT", "PRICE_POSITIVE", "REVERT_UNKNOWN", "UNAVAILABLE",
]
METHOD_MEANING = {
    "NONE": "no signal observed",
    "FREE_LANE_GETTER": "a free-lane getter said this checker has a free mint open",
    "PRICE_ZERO": "price getter(s) returned 0 for this checker",
    "STATIC_WRITE": "mint function ran logic with value 0 (may have reached a state change)",
    "REVERT_GATE": "stopped by an identity / Agent ID / EOA check, no payment error seen",
    "REVERT_PAYMENT": "stopped by a payment error",
    "PRICE_POSITIVE": "a price getter returned more than 0",
    "REVERT_UNKNOWN": "reverted with data that could not be classified",
    "UNAVAILABLE": "mint window closed, sold out, or already claimed",
}

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Function selectors (verified against contracts/MintBeacon.sol).
SEL = dict(
    totalSupply="0x18160ddd", totalMinted="0xa2309ff8", maxSupply="0xd5abeb01",
    MAX_SUPPLY="0x32cb6b0c", mintOpen="0x24bbd049",
    canFreeMint="0xd5b78380", freeLeft="0xeb28217e", freeRemainingOf="0xceda0c40",
    freeFor="0xbde5ccc9", priceFor="0xf12a7add", priceForQty="0xd527979a",
    quoteFor="0x8f79306e", MINT_PRICE="0xc002d23d", PRICE="0x8d859f3e",
    price="0xa035b1fe", currentPrice="0x9d1b464a", basePrice="0xc7876ea4",
    quote="0xed1bd76c", freeMint="0x5b70ea9f", mint="0x1249c58b",
    mintQty="0xa0712d68", claim="0x4e71d92d",
    probeMint="0x0dc26b37", balanceOf="0x70a08231",
    checkerCount="0xfda43269", totalChecks="0x162e2264",
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

OFFCHAIN_SOURCE = "off-chain probe (MintBeacon contract not yet deployed)"
DEPLOYED_SOURCE = "deployed MintBeacon contract"


class RpcError(Exception):
    """A JSON-RPC error response — a definitive answer, not a retryable failure."""

    def __init__(self, err):
        self.err = err
        super().__init__(str(err))


class ToolError(Exception):
    """A tool-level failure, reported to the MCP client with isError=true."""

# ---------------------------------------------------------------------------
# BSC RPC (read-only: eth_call / eth_getCode / eth_estimateGas)
# ---------------------------------------------------------------------------

def _post(payload, tries=8):
    """POST one JSON-RPC payload (a dict, or a list for a batch) to the BSC
    dataseeds with endpoint fallback and backoff.

    A JSON-RPC error inside a single (non-batch) response raises RpcError
    immediately (a revert is a definitive answer); only transport failures
    (timeout / disconnect / bad HTTP / truncated body / HTTP 429) are
    retried across endpoints. HTTP 429 honours Retry-After when present.
    """
    body = json.dumps(payload).encode()
    last = None
    for i in range(tries):
        url = RPCS[i % len(RPCS)]
        try:
            req = urllib.request.Request(url, data=body, headers=RPC_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                try:
                    wait = int(e.headers.get("Retry-After", "0") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 2 ** min(i, 4)), 30))
            else:
                time.sleep(min(2 ** min(i, 4), 15))
        except RpcError:
            raise
        except Exception as e:  # timeout / disconnect / bad JSON / IncompleteRead
            last = e
            time.sleep(min(2 ** min(i, 4), 15))
    raise ToolError(f"BSC RPC failed after {tries} transport tries across "
                    f"{len(RPCS)} endpoints: {last}")


def rpc(method, params, tries=8):
    """Single JSON-RPC call. Returns the result; JSON-RPC errors raise RpcError."""
    j = _post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
              tries=tries)
    if isinstance(j, dict) and "result" in j:
        return j["result"]
    if isinstance(j, dict) and "error" in j:
        raise RpcError(j.get("error"))
    raise ToolError(f"unexpected RPC response for {method}: {j!r}")


def rpc_batch(items, tries=8):
    """Batch JSON-RPC calls in ONE HTTP request (far fewer requests, so the
    public dataseeds rate-limit far less often).

    items: list of (method, params). Returns a list of ("ok", result) or
    ("err", error) tuples in the same order. A transport failure raises
    ToolError. If an endpoint rejects batches, falls back to sequential
    single calls.
    """
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
               for i, (m, p) in enumerate(items)]
    j = _post(payload, tries=tries)
    if not isinstance(j, list):
        # Endpoint does not support batches — do them one by one.
        out = []
        for m, p in items:
            try:
                out.append(("ok", rpc(m, p)))
            except RpcError as e:
                out.append(("err", e.err))
        return out
    by_id = {}
    for item in j:
        if isinstance(item, dict) and "id" in item:
            by_id[item["id"]] = item
    out = []
    for i in range(len(items)):
        item = by_id.get(i)
        if item is None:
            out.append(("err", {"code": -32603, "message": "missing batch item"}))
        elif "result" in item:
            out.append(("ok", item["result"]))
        else:
            out.append(("err", item.get("error")))
    return out


def enc(sel, *args):
    out = sel[2:]
    for a in args:
        if isinstance(a, str) and a.startswith("0x"):
            out += a[2:].lower().rjust(64, "0")
        else:
            out += f"{int(a):064x}"
    return "0x" + out


def eth_call(to, data, frm=PROBE_FROM, gas=500000):
    """Plain eth_call. Returns the returndata hex on success; raises RpcError
    (whose .err holds the revert) on failure."""
    return rpc("eth_call", [{"from": frm, "to": to, "data": data, "value": "0x0",
                             "gas": hex(gas)}, "latest"])


def try_uint(to, data):
    try:
        r = eth_call(to, data)
        if isinstance(r, str) and r.startswith("0x") and len(r) == 66:
            return True, int(r, 16)
    except Exception:
        pass
    return False, 0


def raw_call(to, data):
    """eth_call returning (success, returndata-hex-or-empty).

    The revert data lives in the JSON-RPC error's `data` field — take it
    verbatim; only fall back to message regexes when the endpoint does not
    provide it.
    """
    try:
        r = eth_call(to, data)
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
    except ToolError:
        raise
    except Exception:
        return False, ""


# ---------------------------------------------------------------------------
# Revert classification (mirrors MintBeacon._classifyRevert)
# ---------------------------------------------------------------------------

def decode_reason(ret):
    try:
        b = bytes.fromhex(ret[2:])
        ln = int.from_bytes(b[36:68], "big")
        return b[68:68 + ln].decode("utf-8", "replace")
    except Exception:
        return ""


def classify_revert(ret):
    """Returns (verdict, method) for revert data hex. Never FREE — a revert
    cannot prove freeness."""
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


# ---------------------------------------------------------------------------
# Off-chain classification (mirrors MintBeacon._classify + simulate script)
# ---------------------------------------------------------------------------

def classify_offchain(target, checker):
    """Returns (verdict, method, minted, maxSupply, log).

    Order mirrors the contract: code check -> supply -> mint window ->
    free-lane getters -> price getters -> mint-function probes.
    """
    log = []
    code = rpc("eth_getCode", [target, "latest"])
    if code in ("0x", "", None):
        return ("UNKNOWN", "NONE", 0, 0, log + ["target has no deployed code"])

    # All read-only getters go out in ONE batched JSON-RPC request: the
    # public dataseeds rate-limit per HTTP request, and ~20 sequential calls
    # per verify trips HTTP 429. Order of evaluation still mirrors the
    # contract exactly — only the transport is batched.
    def _call(to, data):
        return {"from": PROBE_FROM, "to": to, "data": data, "value": "0x0",
                "gas": "0x7a120"}  # 500_000

    batch = [
        ("eth_call", [_call(target, enc(SEL["totalSupply"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["totalMinted"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["maxSupply"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["MAX_SUPPLY"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["mintOpen"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["canFreeMint"], checker)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["freeLeft"], checker)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["freeRemainingOf"], checker)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["freeFor"], checker)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["priceFor"], checker)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["priceForQty"], checker, 1)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["quoteFor"], checker, 1)), "latest"]),
        ("eth_call", [_call(target, enc(SEL["MINT_PRICE"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["PRICE"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["price"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["currentPrice"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["basePrice"])), "latest"]),
        ("eth_call", [_call(target, enc(SEL["quote"], 1)), "latest"]),
    ]
    res = rpc_batch(batch)

    def _uint(i):
        status, val = res[i]
        if status == "ok" and isinstance(val, str) and val.startswith("0x") \
                and len(val) == 66:
            return True, int(val, 16)
        return False, 0

    ok, minted = _uint(0)
    if not ok:
        ok, minted = _uint(1)
    ok, mx = _uint(2)
    if not ok:
        ok, mx = _uint(3)
    log.append(f"supply minted={minted} maxSupply={mx}")

    ok, open_ = _uint(4)
    if ok and not open_:
        return ("UNKNOWN", "UNAVAILABLE", minted, mx, log + ["mintOpen=false"])

    # Free-lane getters — the strongest FREE signal, personalised to checker.
    ok, cfm = _uint(5)
    if ok and cfm:
        return ("FREE", "FREE_LANE_GETTER", minted, mx,
                log + ["canFreeMint(checker)=true"])
    for i, name in ((6, "freeLeft"), (7, "freeRemainingOf"), (8, "freeFor")):
        ok, f = _uint(i)
        if ok and f > 0:
            return ("FREE", "FREE_LANE_GETTER", minted, mx,
                    log + [f"{name}(checker)={f}"])

    # Price getters.
    ok, pf = _uint(9)
    if ok and pf == 0:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["priceFor(checker)=0"])
    ok, pfq = _uint(10)
    if ok and pfq == 0:
        return ("FREE", "PRICE_ZERO", minted, mx,
                log + ["priceFor(checker,1)=0"])
    ok, qf = _uint(11)
    if ok and qf == 0:
        return ("FREE", "PRICE_ZERO", minted, mx,
                log + ["quote(checker,1)=0"])
    answered = zero = 0
    paid = 0
    for i, name in ((12, "MINT_PRICE"), (13, "PRICE"), (14, "price"),
                    (15, "currentPrice"), (16, "basePrice"), (17, "quote")):
        ok, p = _uint(i)
        if ok:
            answered += 1
            if p == 0:
                zero += 1
            elif paid == 0:
                paid = p
            log.append(f"{name}={p}")
    if answered > 0 and zero == answered:
        return ("FREE", "PRICE_ZERO", minted, mx, log + ["all price getters 0"])

    # Mint-function probes, most informative first.
    seen = "NONE"
    for name, data in (("freeMint", enc(SEL["freeMint"])),
                       ("mint", enc(SEL["mint"])),
                       ("mint(1)", enc(SEL["mintQty"], 1)),
                       ("claim", enc(SEL["claim"]))):
        ok, ret = raw_call(target, data)
        if ok:
            # eth_call is NOT static: success means the zero-value call
            # would execute. The probe sender holds no Agent ID and is not
            # the checker, so this is LIKELY_FREE, never FREE.
            log.append(f"{name}: SUCCESS with value 0 in simulation")
            return ("LIKELY_FREE", "STATIC_WRITE", minted, mx,
                    log + [f"{name} executed successfully with value 0"])
        if ret in ("", "0x"):
            # Empty revert: use eth_estimateGas as the gas-burn proxy, as in
            # the simulation script. A missing selector reverts for ~300 gas;
            # real logic costs >= 1,000. (estimateGas errors on reverts on
            # most public nodes, so gas is often unknown — then keep probing.)
            try:
                g = int(rpc("eth_estimateGas",
                            [{"from": PROBE_FROM, "to": target, "data": data,
                              "value": "0x0"}, "latest"]), 16)
            except Exception:
                g = -1
            log.append(f"{name}: EMPTY revert, est gas={g}")
            if g >= 1000:
                return ("LIKELY_FREE", "STATIC_WRITE", minted, mx,
                        log + [f"{name} empty revert, gas>=1000"])
            continue
        v, m = classify_revert(ret)
        extra = (f" reason={decode_reason(ret)!r}"
                 if ret[:10].lower() == ERR_STR else "")
        log.append(f"{name}: revert {ret[:74]}... -> {v}/{m}{extra}")
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


# ---------------------------------------------------------------------------
# Deployed-contract path + Agent ID gate check
# ---------------------------------------------------------------------------

def agent_id_gate(checker):
    """Returns (holds, balance-or-None). balanceOf on the Agent ID registry."""
    try:
        ok, bal = try_uint(REGISTRY, enc(SEL["balanceOf"], checker))
        if ok:
            return (bal > 0, bal)
    except Exception:
        pass
    return (None, None)


def probe_deployed(mintbeacon, target, checker):
    """Call probeMint(target, checker) on the deployed contract.

    Returns (verdict, method, minted, maxSupply, log, extra). Raises
    ToolError when the deployed contract cannot be used.
    """
    code = rpc("eth_getCode", [mintbeacon, "latest"])
    if code in ("0x", "", None):
        raise ToolError(f"MINTBEACON_ADDRESS {mintbeacon} has no deployed code "
                        f"on BSC — check the address; falling back is not "
                        f"automatic, unset it to use the off-chain probe.")
    code = rpc("eth_getCode", [target, "latest"])
    if code in ("0x", "", None):
        return ("UNKNOWN", "NONE", 0, 0, ["target has no deployed code"], {})
    try:
        ret = eth_call(mintbeacon, enc(SEL["probeMint"], target, checker),
                       frm=checker, gas=2000000)
    except RpcError as e:
        raise ToolError(f"probeMint on the deployed MintBeacon contract "
                        f"reverted/failed: {e.err}")
    if not (isinstance(ret, str) and ret.startswith("0x") and len(ret) == 258):
        raise ToolError(f"probeMint returned unexpected data: {ret!r}")
    words = [int(ret[2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(4)]
    verdict_i, method_i, minted, mx = words
    if verdict_i >= len(VERDICTS) or method_i >= len(METHODS):
        raise ToolError(f"probeMint returned out-of-range verdict/method: {words}")
    extra = {}
    for label, sel in (("checkerCount", SEL["checkerCount"]),
                       ("totalChecks", SEL["totalChecks"])):
        try:
            ok, v = try_uint(mintbeacon, enc(sel))
            if ok:
                extra[label] = v
        except Exception:
            pass
    return (VERDICTS[verdict_i], METHODS[method_i], minted, mx,
            [f"probeMint called on deployed MintBeacon {mintbeacon}"], extra)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tool_verify_mint(args, mintbeacon):
    if not isinstance(args, dict):
        raise ValueError("verify_mint arguments must be an object")
    target = args.get("contractAddress")
    if not target or not isinstance(target, str) or not ADDR_RE.match(target):
        raise ValueError("contractAddress is required and must be a 0x address "
                         "(40 hex characters), e.g. "
                         "0xff2dac2389a6a9593209b6008d5ffa9e73aa19c2")
    checker_was_provided = bool(args.get("checkerAddress"))
    checker = args.get("checkerAddress") or DEFAULT_CHECKER
    if not isinstance(checker, str) or not ADDR_RE.match(checker):
        raise ValueError("checkerAddress must be a 0x address (40 hex characters)")
    target = target.lower()
    checker = checker.lower()

    holds, balance = agent_id_gate(checker)
    if mintbeacon:
        verdict, method, minted, mx, log, extra = probe_deployed(
            mintbeacon, target, checker)
        source = DEPLOYED_SOURCE
    else:
        verdict, method, minted, mx, log = classify_offchain(target, checker)
        source = OFFCHAIN_SOURCE
        extra = {}

    result = {
        "tool": "verify_mint",
        "contractAddress": target,
        "checkerAddress": checker,
        "checkerAddressProvided": checker_was_provided,
        "checkerAddressNote": (
            None if checker_was_provided else
            "No checkerAddress was supplied. The legacy deployer-wallet default was used; "
            "this result is not personalised to the MCP caller. Pass the actual user's or "
            "agent's wallet address for a personalised result."
        ),
        "verdict": verdict,
        "verdictCode": VERDICTS.index(verdict),
        "verdictMeaning": VERDICT_MEANING[verdict],
        "method": method,
        "methodCode": METHODS.index(method),
        "methodMeaning": METHOD_MEANING[method],
        "minted": minted,
        "maxSupply": mx,
        "agentIdGate": {
            "registry": REGISTRY,
            "checkerAddress": checker,
            "holdsAgentId": holds,
            "balance": balance,
        },
        "source": source,
        "mintbeaconAddress": mintbeacon,
        "probeLog": log,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }
    result.update(extra)
    return result


def tool_mintbeacon_status(mintbeacon):
    """Return live BSC deployment metadata and on-chain usage counters."""
    if not mintbeacon:
        raise ToolError("MintBeacon deployment is not configured")
    code = rpc("eth_getCode", [mintbeacon, "latest"])
    if code in ("0x", "", None):
        raise ToolError(f"MintBeacon address {mintbeacon} has no deployed code on BSC")
    chain_hex = rpc("eth_chainId", [])
    try:
        chain_id = int(chain_hex, 16)
    except (TypeError, ValueError):
        raise ToolError(f"BSC RPC returned an invalid chain ID: {chain_hex!r}")
    registry_code = rpc("eth_getCode", [REGISTRY, "latest"])
    ok_holders, holders = try_uint(mintbeacon, enc(SEL["checkerCount"]))
    ok_checks, checks = try_uint(mintbeacon, enc(SEL["totalChecks"]))
    return {
        "tool": "mintbeacon_status",
        "network": "BNB Smart Chain Mainnet" if chain_id == 56 else "Unexpected EVM network",
        "chainId": chain_id,
        "expectedChainId": 56,
        "networkMatchesBscMainnet": chain_id == 56,
        "contractAddress": mintbeacon,
        "contractCodePresent": True,
        "contractSourceVerification": "verified on BscScan",
        "contractExplorerUrl": f"https://bscscan.com/address/{mintbeacon}",
        "websiteUrl": "https://mintbeacon.vercel.app",
        "agentIdRegistry": REGISTRY,
        "agentIdRegistryCodePresent": registry_code not in ("0x", "", None),
        "distinctAgentIdHolders": holders if ok_holders else None,
        "totalRecordedChecks": checks if ok_checks else None,
        "counterDefinitions": {
            "distinctAgentIdHolders": (
                "Unique Agent ID-holding addresses that have successfully recorded "
                "at least one on-chain check. Repeated checks by one address count once."
            ),
            "totalRecordedChecks": (
                "Total successful on-chain checkMint transactions. Read-only MCP "
                "verification calls do not increment this counter."
            ),
        },
        "mcpTransactionPolicy": (
            "All MCP tools are read-only. They never sign or send transactions. "
            "The website's optional on-chain check requires the user's own wallet approval."
        ),
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }


def tool_free_mints_feed(args):
    if not isinstance(args, dict):
        args = {}
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    limit = max(1, min(100, limit))
    try:
        req = urllib.request.Request(
            FEED_URL, headers={"User-Agent": "Mozilla/5.0 MintBeacon-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            feed = json.load(r)
    except Exception as e:
        raise ToolError(f"could not fetch the free-mints feed at {FEED_URL}: {e}")
    projects = feed.get("projects")
    if not isinstance(projects, list):
        raise ToolError(f"feed at {FEED_URL} has no 'projects' list — "
                        f"format may have changed; not inventing a list")
    free = [p for p in projects if isinstance(p, dict) and p.get("status") == "free_live"]
    out = []
    for p in free[:limit]:
        out.append({
            "name": p.get("name"),
            "contractAddress": p.get("contract"),
            "minted": p.get("minted"),
            "maxSupply": p.get("max_supply"),
            "status": p.get("status"),
        })
    return {
        "tool": "free_mints_feed",
        "source": FEED_URL,
        "feedUpdatedAt": feed.get("updated_at"),
        "count": len(out),
        "freeLiveTotal": len(free),
        "limit": limit,
        "projects": out,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


TOOLS = [
    {
        "name": "mintbeacon_status",
        "description": (
            "Inspect the live MintBeacon deployment on BNB Smart Chain: chain ID, "
            "contract address, Agent ID registry, distinct Agent ID holder count and "
            "total successful on-chain checks. This is read-only and does not increment "
            "either counter. No wallet address is required."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {
            "title": "MintBeacon deployment status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "verify_mint",
        "description": (
            "Verify whether a BSC contract looks like a FREE, gas-only Agent ID "
            "mint for the supplied checker address. Always supply the actual user's or "
            "agent's wallet; omission uses a legacy deployer-wallet default and is not "
            "personalised to the MCP caller. Returns verdict (UNKNOWN/FREE/"
            "LIKELY_FREE/PAID), method, minted/maxSupply, and whether the "
            "checker holds an Agent ID. LIKELY_FREE means 'probably free — "
            "deep-check recommended', never a guarantee. Read-only: never "
            "signs or sends transactions. When the MintBeacon contract is not "
            "deployed, runs an off-chain probe against the target and says so."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contractAddress": {
                    "type": "string",
                    "description": "BSC contract address to verify (0x, 40 hex characters).",
                },
                "checkerAddress": {
                    "type": "string",
                    "description": "Address the verdict is personalised for. Always pass "
                                   "the actual user's or agent's wallet for a personalised "
                                   "result. Optional only for backward compatibility; omission "
                                   "uses the deployer-wallet default and does not identify the caller.",
                },
            },
            "required": ["contractAddress"],
        },
        "annotations": {
            "title": "Verify a BNB Chain mint",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "free_mints_feed",
        "description": (
            "List currently free, live Agent ID mints from the MINTS feed "
            "(mints-bnb.pages.dev). Returns name, contract address and "
            "minted/supply when present. This is a curated feed, not a "
            "MintBeacon verification — run verify_mint on a contract before "
            "acting on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum projects to return (default 20, max 100).",
                    "default": 20,
                },
            },
            "required": [],
        },
        "annotations": {
            "title": "List live free mints",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
]


# ---------------------------------------------------------------------------
# MCP over stdio (JSON-RPC 2.0, newline-delimited)
# ---------------------------------------------------------------------------

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def err_result(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def tool_error_text(message):
    return {"content": [{"type": "text", "text": json.dumps(
        {"error": message}, indent=2)}], "isError": True}


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
MINTBEACON_INSTRUCTIONS = (
    "MintBeacon is a read-only BNB Smart Chain mainnet mint verifier. "
    "Call mintbeacon_status to inspect the live deployment and counters. "
    "Call verify_mint with the target contractAddress and, whenever possible, "
    "the actual user's or agent's own checkerAddress; if omitted, the legacy "
    "deployer-wallet default is used and the result is not personalised to the caller. "
    "verify_mint uses eth_call and does not create a transaction or increment counters. "
    "The separate website offers an optional user-approved on-chain check; this MCP "
    "server never signs or sends transactions."
)


def _modern_result(result):
    """Add required 2026-07-28 response metadata to a successful result."""
    if not isinstance(result, dict):
        result = {}
    result.setdefault("resultType", "complete")
    result["_meta"] = {SERVER_INFO_META_KEY: {"name": SERVER_NAME, "version": VERSION}}
    return result


def _handle_modern_request(req, mintbeacon):
    params = req.get("params") or {}
    if not isinstance(params, dict):
        return err_result(req.get("id"), -32602, "params must be an object")
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return err_result(req.get("id"), -32602, "modern MCP requests require params._meta")
    requested = meta.get(PROTOCOL_VERSION_META_KEY)
    if requested != MODERN_PROTOCOL_VERSION:
        return {
            "jsonrpc": "2.0", "id": req.get("id"),
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {
                    "supported": [MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION],
                    "requested": requested,
                },
            },
        }
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, dict):
        return err_result(req.get("id"), -32602,
                          "params._meta.io.modelcontextprotocol/clientCapabilities must be an object")

    method = req.get("method", "")
    req_id = req.get("id")
    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "resultType": "complete",
            "supportedVersions": [MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION],
            "capabilities": {"tools": {}},
            "_meta": {SERVER_INFO_META_KEY: {"name": SERVER_NAME, "version": VERSION}},
            "instructions": MINTBEACON_INSTRUCTIONS,
            "ttlMs": 60000,
            "cacheScope": "public",
        }}

    response = None
    if method == "tools/list":
        response = {"jsonrpc": "2.0", "id": req_id, "result": {
            "tools": TOOLS,
            "resultType": "complete",
            "ttlMs": 60000,
            "cacheScope": "public",
        }}
    elif method == "tools/call":
        response = _handle_tool_call(req, mintbeacon)
    else:
        return err_result(req_id, -32601, f"method not found: {method}")

    if response is not None and "result" in response:
        response["result"] = _modern_result(response["result"])
    return response


def _handle_tool_call(req, mintbeacon):
    """Shared tools/call implementation for legacy and modern MCP requests."""
    params = req.get("params") or {}
    req_id = req.get("id")
    if not isinstance(params, dict):
        return err_result(req_id, -32602, "params must be an object")
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return err_result(req_id, -32602, "tool arguments must be an object")
    if name not in ("mintbeacon_status", "verify_mint", "free_mints_feed"):
        return err_result(req_id, -32602, f"unknown tool: {name!r}")
    try:
        if name == "mintbeacon_status":
            result = tool_mintbeacon_status(mintbeacon)
        elif name == "verify_mint":
            result = tool_verify_mint(args, mintbeacon)
        else:
            result = tool_free_mints_feed(args)
    except ValueError as e:
        return err_result(req_id, -32602, str(e))
    except ToolError as e:
        return {"jsonrpc": "2.0", "id": req_id, "result": tool_error_text(str(e))}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": req_id,
                "result": tool_error_text(f"tool {name} failed: {e}")}
    return {"jsonrpc": "2.0", "id": req_id, "result": {
        "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
    }}


def handle_request(req, mintbeacon):
    """Returns a response object, or None for notifications (no reply)."""
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
        return err_result(req.get("id") if isinstance(req, dict) else None,
                          -32600, "invalid JSON-RPC 2.0 request")
    method = req.get("method", "")
    req_id = req.get("id")
    if not isinstance(method, str) or not method:
        return err_result(req_id, -32600, "method must be a non-empty string")
    is_notification = "id" not in req or req_id is None
    params = req.get("params", {})
    if not isinstance(params, dict):
        return err_result(req_id, -32602, "params must be an object")
    request_meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(request_meta, dict) and (
        PROTOCOL_VERSION_META_KEY in request_meta
        or CLIENT_CAPABILITIES_META_KEY in request_meta
        or method == "server/discover"
    ):
        return _handle_modern_request(req, mintbeacon)
    if method.startswith("notifications/"):
        return None  # e.g. notifications/initialized — no reply

    if method == "initialize":
        params = req.get("params") or {}
        requested_version = params.get("protocolVersion")
        supported_legacy_versions = (LEGACY_PROTOCOL_VERSION, "2024-11-05")
        negotiated_version = (
            requested_version
            if requested_version in supported_legacy_versions
            else LEGACY_PROTOCOL_VERSION
        )
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": negotiated_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": MINTBEACON_INSTRUCTIONS,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"tools": TOOLS}}
    if method == "tools/call":
        if is_notification:
            return None
        return _handle_tool_call(req, mintbeacon)
    return err_result(req_id, -32601, f"method not found: {method}")


def main():
    ap = argparse.ArgumentParser(
        description="MintBeacon MCP server (stdio). Reads the deployed "
                    "MintBeacon address from --mintbeacon or MINTBEACON_ADDRESS; "
                    "without it, verify_mint runs the off-chain probe.")
    ap.add_argument("--mintbeacon", default=None,
                    help="deployed MintBeacon contract address (overrides "
                         "MINTBEACON_ADDRESS)")
    args = ap.parse_args()
    mintbeacon = (args.mintbeacon or os.environ.get("MINTBEACON_ADDRESS")
                  or "0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B")  # deployed 2026-11-25
    if mintbeacon:
        mintbeacon = mintbeacon.strip()
        if not ADDR_RE.match(mintbeacon):
            print(f"warning: ignoring malformed MINTBEACON_ADDRESS "
                  f"{mintbeacon!r}; running in off-chain probe mode",
                  file=sys.stderr)
            mintbeacon = None
        else:
            mintbeacon = mintbeacon.lower()
    else:
        print("MintBeacon contract not deployed yet (no MINTBEACON_ADDRESS): "
              "verify_mint will run the off-chain probe.", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            send(err_result(None, -32700, f"parse error: {e}"))
            continue
        try:
            resp = handle_request(req, mintbeacon)
        except Exception as e:
            resp = err_result(req.get("id") if isinstance(req, dict) else None,
                              -32603, f"internal error: {e}")
        if resp is not None:
            send(resp)
    # EOF on stdin: clean shutdown.
    return 0


if __name__ == "__main__":
    sys.exit(main())
