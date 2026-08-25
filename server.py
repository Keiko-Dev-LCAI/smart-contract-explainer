#!/usr/bin/env python3
"""
Smart Contract Explainer - Backend Server
Serves the HTML app and proxies Etherscan/Lightscan API calls.

Production: AIVM-only (Lightchain decentralized inference).
Set LIGHTCHAIN_PRIVATE_KEY (wallet on chain 9200 with LCAI). ~0.022 LCAI/query.

  export LIGHTCHAIN_PRIVATE_KEY=0x...
  python3 server.py
"""

import os
import sys

# ── Bootstrap package paths (local pylibs + app dir on Railway) ───────────
sys.path.insert(0, os.path.join(os.path.expanduser('~'), 'pylibs'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import json
import secrets
import base64
import threading
import time
import uuid as _uuid_mod
from urllib.parse import urlparse, parse_qs, quote as url_quote

# ── Config ──────────────────────────────────────────────────────────────
PORT = int(os.environ.get('PORT', '8080'))
HOME = os.path.expanduser('~')
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.environ.get('SCE_STATIC_DIR') or (
    _SCRIPT_DIR
    if os.path.isfile(os.path.join(_SCRIPT_DIR, 'contract-explainer.html'))
    else HOME
)
# AIVM_ONLY=1 (default): no Ollama fallback in production
AIVM_ONLY = os.environ.get('AIVM_ONLY', '1').strip() not in ('0', 'false', 'False', 'no')

ETHERSCAN_KEY = "V2TNJIG3PY8K6R2WDHAQ3RHIYV3DM7K3JD"

# Etherscan V2 uses one endpoint with chainid param
ETHERSCAN_V2 = 'https://api.etherscan.io/v2/api'

# V1-style per-chain API URLs (tokenlist/tokentx not supported in V2)
CHAIN_API_V1 = {
    'eth':        'https://api.etherscan.io/api',
    'bsc':        'https://api.bscscan.com/api',
    'polygon':    'https://api.polygonscan.com/api',
    'arbitrum':   'https://api.arbiscan.io/api',
    'lightchain': 'https://mainnet.lightscan.app/api',
}

CHAIN_IDS = {
    'eth':        1,
    'bsc':        56,
    'polygon':    137,
    'arbitrum':   42161,
    'lightchain': None,   # uses own explorer
}

LIGHTCHAIN_API = 'https://mainnet.lightscan.app/api'

CHAIN_RPC = {
    'eth':        ['https://cloudflare-eth.com', 'https://rpc.ankr.com/eth', 'https://ethereum.publicnode.com'],
    'bsc':        ['https://bsc-dataseed.binance.org', 'https://bsc-dataseed1.defibit.io'],
    'polygon':    ['https://polygon-rpc.com', 'https://rpc.ankr.com/polygon'],
    'arbitrum':   ['https://arb1.arbitrum.io/rpc', 'https://rpc.ankr.com/arbitrum'],
    'lightchain': ['https://rpc.mainnet.lightchain.ai'],
}
# ────────────────────────────────────────────────────────────────────────

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css':  'text/css',
    '.js':   'application/javascript',
    '.json': 'application/json',
    '.png':  'image/png',
    '.ico':  'image/x-icon',
}


# ════════════════════════════════════════════════════════════════════════
# AIVM CLIENT — Lightchain Decentralized Inference
# ════════════════════════════════════════════════════════════════════════

AIVM_GATEWAY = "https://chat-api.mainnet.lightchain.ai"
AIVM_RELAY   = "wss://relay.mainnet.lightchain.ai/ws"
AIVM_RPC     = "https://rpc.mainnet.lightchain.ai"
AIVM_JOB_REG = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
AIVM_JOB_FEE = 20_000_000_000_000_000   # 0.02 LCAI in wei
AIVM_CHAIN_ID = 9200

AIVM_ABI = [
    {
        "name": "createSession", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "paramsHash",     "type": "bytes32"},
            {"name": "worker",         "type": "address"},
            {"name": "encWorkerKey",   "type": "bytes"},
            {"name": "ephemeralPubKey","type": "bytes"},
            {"name": "initState",      "type": "bytes"},
            {"name": "expiry",         "type": "uint256"},
        ],
        "outputs": [{"name": "sessionId", "type": "uint256"}],
    },
    {
        "name": "submitJob", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "sessionId",  "type": "uint256"},
            {"name": "promptHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "jobId", "type": "uint256"}],
    },
    {
        "anonymous": False, "name": "SessionCreated", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "sessionId",     "type": "uint256"},
            {"indexed": True,  "name": "user",           "type": "address"},
            {"indexed": True,  "name": "paramsHash",     "type": "bytes32"},
            {"indexed": False, "name": "worker",         "type": "address"},
            {"indexed": False, "name": "encWorkerKey",   "type": "bytes"},
            {"indexed": False, "name": "ephemeralPubKey","type": "bytes"},
        ],
    },
    {
        "anonymous": False, "name": "JobSubmitted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",     "type": "uint256"},
            {"indexed": True,  "name": "sessionId", "type": "uint256"},
            {"indexed": False, "name": "worker",    "type": "address"},
        ],
    },
    {
        "anonymous": False, "name": "JobCompleted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",          "type": "uint256"},
            {"indexed": True,  "name": "worker",          "type": "address"},
            {"indexed": False, "name": "responseHash",    "type": "bytes32"},
            {"indexed": False, "name": "ciphertextHash",  "type": "bytes32"},
        ],
    },
]


def _decode_pubkey(s):
    """Accept hex (with/without 0x) or base64; return 65-byte uncompressed P-256 point."""
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = s.strip()
    if s.startswith('0x') or s.startswith('0X'):
        b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in '0123456789abcdefABCDEF' for c in s):
        b = bytes.fromhex(s)
    else:
        b = base64.b64decode(s)
    if len(b) != 65:
        raise ValueError(f"pubkey decode: expected 65 bytes, got {len(b)}")
    return b


def _ecdh_wrap(session_key: bytes, peer_pub_bytes: bytes) -> bytes:
    """
    ECDH-wrap session_key for peer P-256 pubkey.
    Returns: ephemPub(65) || nonce(12) || ct || tag(16)
    Raw ECDH X-coordinate used directly as AES-256 key (no KDF — protocol requirement).
    """
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend

    x = int.from_bytes(peer_pub_bytes[1:33], 'big')
    y = int.from_bytes(peer_pub_bytes[33:65], 'big')
    peer_pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())

    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared = ephem_priv.exchange(ECDH(), peer_pub)   # 32-byte X coord, raw = AES key

    pub_nums = ephem_priv.public_key().public_numbers()
    ephem_pub_bytes = (b'\x04' +
                       pub_nums.x.to_bytes(32, 'big') +
                       pub_nums.y.to_bytes(32, 'big'))

    nonce  = secrets.token_bytes(12)
    ct_tag = AESGCM(shared).encrypt(nonce, session_key, None)   # 32-byte key → 48-byte ct+tag
    return ephem_pub_bytes + nonce + ct_tag


def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aes_decrypt(key: bytes, blob: bytes) -> bytes:
    """AES-256-GCM decrypt nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


# Serialize on-chain AIVM txs so concurrent /api/analyze jobs don't collide on nonce
_aivm_tx_lock = threading.Lock()


class AIVMClient:
    """
    Runs LLM inference through the Lightchain decentralized worker network.
    Requires a funded Lightchain mainnet wallet (chain ID 9200, LCAI balance).
    Cost: ~0.022 LCAI per inference (0.02 worker fee + ~0.002 gas).
    """

    def __init__(self, private_key: str):
        import requests as _req
        from web3 import Web3
        from eth_account import Account

        self._req      = _req
        self._w3       = Web3(Web3.HTTPProvider(AIVM_RPC))
        self._account  = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(AIVM_JOB_REG),
            abi=AIVM_ABI,
        )
        self._jwt     = None
        self._jwt_exp = 0
        self._local_nonce = None  # track pending nonce while holding _aivm_tx_lock
        print(f"  [AIVM] wallet: {self._account.address}")

    def _next_nonce(self) -> int:
        """Prefer pending count; keep a local bump while we own the tx lock."""
        pending = self._w3.eth.get_transaction_count(self._account.address, 'pending')
        latest = self._w3.eth.get_transaction_count(self._account.address, 'latest')
        base = max(pending, latest)
        if self._local_nonce is None or self._local_nonce < base:
            self._local_nonce = base
        n = self._local_nonce
        self._local_nonce = n + 1
        return n

    def _gas_price(self, bump: float = 1.25) -> int:
        gp = int(self._w3.eth.gas_price or 0)
        if gp <= 0:
            gp = 1_000_000_000  # 1 gwei fallback
        return max(int(gp * bump), gp + 1)

    def _send_contract_tx(self, built_fn, *, gas: int, value: int = 0, label: str = "tx"):
        """
        Sign + send with retries for 'replacement transaction underpriced' / nonce races.
        Caller should hold _aivm_tx_lock for the whole multi-tx inference when possible.
        """
        last_err = None
        gas_mult = 1.35
        for attempt in range(5):
            try:
                nonce = self._next_nonce()
                # rewind local nonce if we need to retry same attempt — bump gas instead
                gas_price = self._gas_price(gas_mult)
                tx = built_fn.build_transaction({
                    "from":     self._account.address,
                    "nonce":    nonce,
                    "gas":      gas,
                    "gasPrice": gas_price,
                    "value":    value,
                    "chainId":  AIVM_CHAIN_ID,
                })
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  [AIVM] {label} tx: {tx_hash.hex()} (nonce={nonce} gasPrice={gas_price} attempt={attempt+1})")
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt.status != 1:
                    raise RuntimeError(f"{label} reverted on-chain")
                return receipt
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                # allow retry: underpriced, already known, nonce too low
                retryable = any(s in msg for s in (
                    'underpriced', 'replacement transaction', 'nonce too low',
                    'already known', 'alreadyimported', 'already known',
                ))
                print(f"  [AIVM] {label} send failed attempt {attempt+1}: {e}")
                if not retryable or attempt == 4:
                    break
                # reset local nonce from chain and bump gas harder
                self._local_nonce = None
                gas_mult *= 1.4
                time.sleep(1.2 + attempt * 0.5)
        raise RuntimeError(f"AIVM {label} failed after retries: {last_err}")

    def _get_jwt(self) -> str:
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30:
            return self._jwt
        r = self._req.get(
            f"{AIVM_GATEWAY}/api/auth/challenge",
            params={"address": self._account.address}, timeout=15,
        )
        r.raise_for_status()
        message = r.json()["message"]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = self._req.post(
            f"{AIVM_GATEWAY}/api/auth/verify",
            json={"message": message, "signature": "0x" + sig.signature.hex()},
            timeout=15,
        )
        r2.raise_for_status()
        v = r2.json()
        self._jwt = v["token"]
        # Parse ISO timestamp (trim sub-seconds)
        exp_str = v["expiresAt"][:19].replace("T", " ")
        self._jwt_exp = time.mktime(time.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        return self._jwt

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

    def run_inference(self, prompt: str, timeout_secs: int = 360) -> str:
        """Serialize full inference so concurrent web requests don't fight over nonces."""
        with _aivm_tx_lock:
            self._local_nonce = None
            return self._run_inference_locked(prompt, timeout_secs)

    def _run_inference_locked(self, prompt: str, timeout_secs: int = 360) -> str:
        import websocket as _ws
        from web3 import Web3

        req = self._req
        print(f"  [AIVM] starting inference ({len(prompt)} chars)")

        # ── 1-2. Auth + pick model ─────────────────────────────────────
        r = req.get(f"{AIVM_GATEWAY}/api/models", timeout=15)
        r.raise_for_status()
        models = r.json().get("models", [])
        model  = next((m for m in models if m["name"] == "llama3-8b"), models[0] if models else None)
        if not model:
            raise RuntimeError("No models available from AIVM gateway")
        model_id = model["id"]   # "0x<64 hex chars>"
        print(f"  [AIVM] model: {model['name']} id={model_id[:10]}…")

        # ── 3. Select worker ───────────────────────────────────────────
        r = req.post(
            f"{AIVM_GATEWAY}/api/sessions/select",
            json={"modelId": model_id},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        sel = r.json()
        print(f"  [AIVM] worker: {sel['worker']}")

        # ── 4-5. Session key + ECDH wrap ───────────────────────────────
        session_key   = secrets.token_bytes(32)
        enc_worker    = _ecdh_wrap(session_key, _decode_pubkey(sel["workerEncryptionKey"]))
        enc_disputer  = _ecdh_wrap(session_key, _decode_pubkey(sel["disputerEncryptionKey"]))

        # ── 6. Prepare (get dispatcher signature) ─────────────────────
        r = req.post(
            f"{AIVM_GATEWAY}/api/sessions/prepare",
            json={
                "modelId":        model_id,
                "encWorkerKey":   base64.b64encode(enc_worker).decode(),
                "encDisputerKey": base64.b64encode(enc_disputer).decode(),
            },
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        prep = r.json()

        # ── 7. createSession on-chain ──────────────────────────────────
        # Exact arg mapping per protocol spec (ephemeralPubKey slot gets disputer blob)
        params_hash = bytes.fromhex(model_id[2:].zfill(64) if model_id[:2].lower() == "0x" else model_id.zfill(64))
        sig_bytes   = bytes.fromhex(prep["signature"][2:] if prep["signature"][:2].lower() == "0x" else prep["signature"])

        receipt1 = self._send_contract_tx(
            self._registry.functions.createSession(
                params_hash,
                Web3.to_checksum_address(prep["worker"]),
                enc_worker,
                enc_disputer,   # → ephemeralPubKey slot (protocol requirement)
                sig_bytes,
                prep["expiry"],
            ),
            gas=1_000_000,
            value=0,
            label="createSession",
        )

        session_id = None
        for log in receipt1.logs:
            try:
                evt = self._registry.events.SessionCreated().process_log(log)
                session_id = evt["args"]["sessionId"]
                break
            except Exception:
                pass
        if session_id is None:
            raise RuntimeError("SessionCreated event not found in receipt")
        print(f"  [AIVM] sessionId: {session_id}")

        # ── 8. Open relay BEFORE submitting job ────────────────────────
        relay_token = None
        deadline = time.time() + 30
        while time.time() < deadline:
            r = req.get(
                f"{AIVM_GATEWAY}/api/sessions/{session_id}/token",
                headers=self._auth_headers(), timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("token"):
                    relay_token = d["token"]
                    break
            time.sleep(1)
        if not relay_token:
            raise RuntimeError("Relay token not ready within 30s")

        chunks    = []
        ws_ready  = threading.Event()
        ws_err    = [None]

        def _on_message(ws_obj, message):
            try:
                frame = json.loads(message)
                payload = frame.get("payload")
                if not payload:
                    return
                blob = base64.b64decode(payload)
                try:
                    pt = _aes_decrypt(session_key, blob)
                    chunks.append(pt.decode("utf-8", errors="replace"))
                except Exception:
                    pass   # skip undecryptable control frames
            except Exception:
                pass

        def _on_open(ws_obj):
            ws_ready.set()

        def _on_error(ws_obj, err):
            ws_err[0] = err
            ws_ready.set()

        ws = _ws.WebSocketApp(
            f"{AIVM_RELAY}?token={url_quote(relay_token)}",
            on_message=_on_message,
            on_open=_on_open,
            on_error=_on_error,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0]:
            raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        print("  [AIVM] relay connected")

        # ── 9. Encrypt prompt + upload blob ────────────────────────────
        cipher = _aes_encrypt(session_key, prompt.encode("utf-8"))
        r = req.post(
            f"{AIVM_GATEWAY}/api/blobs",
            json={"data": base64.b64encode(cipher).decode()},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        blob_hashes = r.json().get("blobHashes", [])
        if not blob_hashes:
            raise RuntimeError("No blob hash returned from gateway")
        _bh = blob_hashes[0]
        prompt_hash = bytes.fromhex(_bh[2:].zfill(64) if _bh[:2].lower() == "0x" else _bh.zfill(64))

        # ── 10. submitJob (pay 0.02 LCAI) ─────────────────────────────
        receipt2 = self._send_contract_tx(
            self._registry.functions.submitJob(session_id, prompt_hash),
            gas=500_000,
            value=AIVM_JOB_FEE,
            label="submitJob",
        )

        job_id = None
        for log in receipt2.logs:
            try:
                evt = self._registry.events.JobSubmitted().process_log(log)
                job_id = evt["args"]["jobId"]
                break
            except Exception:
                pass
        if job_id is None:
            raise RuntimeError("JobSubmitted event not found in receipt")
        print(f"  [AIVM] jobId: {job_id}")

        # ── 11. Poll for JobCompleted ──────────────────────────────────
        job_completed_topic = "0x" + Web3.keccak(
            text="JobCompleted(uint256,address,bytes32,bytes32)"
        ).hex()
        job_id_topic = "0x" + hex(job_id)[2:].zfill(64)

        done     = False
        deadline = time.time() + timeout_secs
        while time.time() < deadline and not done:
            time.sleep(5)
            try:
                head = self._w3.eth.block_number
                logs = self._w3.eth.get_logs({
                    "address":   Web3.to_checksum_address(AIVM_JOB_REG),
                    "fromBlock": receipt2.blockNumber,
                    "toBlock":   head,
                    "topics":    [job_completed_topic, job_id_topic],
                })
                if logs:
                    done = True
                    print(f"  [AIVM] JobCompleted! worker: {logs[0].get('address')}")
            except Exception as e:
                print(f"  [AIVM] log poll error (retrying): {e}")

        time.sleep(4)   # grace period for final relay frames
        ws.close()

        result = "".join(chunks)
        if result:
            # Got relay data even if JobCompleted event didn't fire — return it
            print(f"  [AIVM] inference done (relay data), {len(result)} chars")
            return result

        if not done:
            raise RuntimeError(f"Timeout after {timeout_secs}s waiting for JobCompleted")

        print(f"  [AIVM] inference done, {len(result)} chars received")
        return result


# ── LCAI price feed (cached 5 min) ──────────────────────────────────────
_lcai_price_cache = {'price': None, 'ts': 0}

def _get_lcai_price_usd():
    """Fetch LCAI/USD price. Tries CoinGecko then DexScreener; caches 5 min."""
    global _lcai_price_cache
    if time.time() - _lcai_price_cache['ts'] < 300 and _lcai_price_cache['price']:
        return _lcai_price_cache['price']

    # ── 1. CoinGecko ──────────────────────────────────────────────────────
    for cg_id in ('lightchain-ai', 'lightchain'):
        try:
            req = urllib.request.Request(
                f'https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd',
                headers={'Accept': 'application/json', 'User-Agent': 'SmartContractExplainer/1.0'},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            price = data.get(cg_id, {}).get('usd')
            if price and float(price) > 0:
                _lcai_price_cache = {'price': float(price), 'ts': time.time()}
                print(f'  [price] CoinGecko ({cg_id}): ${price}')
                return float(price)
        except Exception as e:
            print(f'  [price] CoinGecko ({cg_id}) failed: {e}')

    # ── 2. DexScreener ────────────────────────────────────────────────────
    try:
        req = urllib.request.Request(
            'https://api.dexscreener.com/latest/dex/search?q=LCAI',
            headers={'Accept': 'application/json', 'User-Agent': 'SmartContractExplainer/1.0'},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        for pair in data.get('pairs', []):
            if pair.get('baseToken', {}).get('symbol', '').upper() == 'LCAI':
                price = float(pair.get('priceUsd', 0))
                if price > 0:
                    _lcai_price_cache = {'price': price, 'ts': time.time()}
                    print(f'  [price] DexScreener: ${price}')
                    return price
    except Exception as e:
        print(f'  [price] DexScreener failed: {e}')

    # ── 3. Fallback (stale cache or last known) ───────────────────────────
    if _lcai_price_cache['price']:
        print(f'  [price] Using stale cache: ${_lcai_price_cache["price"]}')
        return _lcai_price_cache['price']

    print('  [price] All sources failed, returning 0.004 fallback')
    return 0.004   # last-resort fallback

# ── On-chain paid-access check ──────────────────────────────────────────
ACCESS_CONTRACT = '0xd5E2d7b5F55dd0B21D5Bf9319801FC38489A476d'
# keccak256("hasAccess(address)")[:4] = 0x95a078e8
_HAS_ACCESS_SELECTOR = '95a078e8'

def _verify_paid_access(wallet: str) -> bool:
    """
    Call hasAccess(wallet) on the ContractExplainerAccess contract via Lightchain RPC.
    Returns True if the wallet has active paid access, False otherwise.
    Result is ABI-encoded bool: 0x000...001 = true, 0x000...000 = false.
    """
    if not wallet or len(wallet) != 42 or not wallet.startswith('0x'):
        return False
    try:
        addr_padded = wallet[2:].lower().zfill(64)
        call_data   = '0x' + _HAS_ACCESS_SELECTOR + addr_padded
        payload = json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'eth_call',
            'params': [{'to': ACCESS_CONTRACT, 'data': call_data}, 'latest'],
        }).encode()
        req = urllib.request.Request(
            'https://rpc.mainnet.lightchain.ai',
            data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'SmartContractExplainer/1.0'},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            result = json.loads(r.read()).get('result', '0x')
        # 66-char string: 0x + 64 hex chars; last char is 1=true, 0=false
        return isinstance(result, str) and result.endswith('1')
    except Exception as e:
        print(f'  [access] on-chain check failed: {e}')
        return False

# ── Lazy singleton ───────────────────────────────────────────────────────
_aivm_client = None

def get_aivm_client():
    """Return AIVMClient if LIGHTCHAIN_PRIVATE_KEY is set, else None."""
    global _aivm_client
    pk = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()
    if not pk:
        return None
    if _aivm_client is None:
        try:
            _aivm_client = AIVMClient(pk)
        except Exception as e:
            print(f"  [AIVM] Failed to init client: {e}")
            return None
    return _aivm_client


# Known contracts — IDENTITY only for UI logos + prompt context.
# Never use these entries to force a risk rating. Risk comes only from source analysis.
KNOWN_CONTRACTS = {
    # ── Lightchain ──────────────────────────────────────────────────────
    "0x9ca8530ca349c966fe9ef903df17a75b8a778927": {
        "name": "Lightchain AI (LCAI)",
        "symbol": "LCAI",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/lcai-mark.svg",
        "blurb": (
            "Official Lightchain AI ERC-20 token contract on Ethereum (identity). "
            "Not the Lightchain protocol Treasury. Protocol spending is governed separately "
            "via DAO Governor + Timelock on Lightchain mainnet "
            "(Treasury 0x786eDe8C42Ca54E54c9dCECa9b30052CF4743389)."
        ),
    },
    "0x786ede8c42ca54e54c9dceca9b30052cf4743389": {
        "name": "Lightchain Treasury",
        "symbol": None,
        "chain": "lightchain",
        "kind": "treasury",
        "logo": "/known-logos/treasury-mark.svg",
        "blurb": (
            "On-chain Treasury for Lightchain AI mainnet (identity). "
            "Intended to be controlled by DAO Governor + 48-hour Timelock — not a personal wallet."
        ),
    },
    # ── Ethereum blue-chips (identity only) ─────────────────────────────
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
        "name": "USD Coin",
        "symbol": "USDC",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/usdc-mark.svg",
        "blurb": (
            "Circle USD Coin (USDC) on Ethereum mainnet (identity). "
            "Widely used fiat-backed stablecoin; often implemented behind a proxy/upgrade pattern. "
            "Identify from code; do not invent Circle legal claims beyond what source shows."
        ),
    },
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {
        "name": "Tether USD",
        "symbol": "USDT",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/usdt-mark.svg",
        "blurb": (
            "Tether USD (USDT) on Ethereum mainnet (identity). "
            "Legacy ERC-20-style stablecoin with issuer admin surfaces common in its design era."
        ),
    },
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
        "name": "Wrapped Ether",
        "symbol": "WETH",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/weth-mark.svg",
        "blurb": (
            "Wrapped Ether (WETH) on Ethereum mainnet (identity). "
            "Canonical deposit/withdraw wrapper: ETH in → WETH out, and reverse. Not a governance token."
        ),
    },
    "0x6b175474e89094c44da98b954eedeac495271d0f": {
        "name": "Dai Stablecoin",
        "symbol": "DAI",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/dai-mark.svg",
        "blurb": (
            "MakerDAO Dai (DAI) on Ethereum mainnet (identity). "
            "Decentralized stablecoin token; protocol risk is broader than this ERC-20 surface alone."
        ),
    },
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": {
        "name": "Uniswap",
        "symbol": "UNI",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/uni-mark.svg",
        "blurb": (
            "Uniswap governance token (UNI) on Ethereum mainnet (identity). "
            "ERC-20 used for Uniswap governance — not the swap router itself."
        ),
    },
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": {
        "name": "Uniswap V2 Router",
        "symbol": None,
        "chain": "eth",
        "kind": "router",
        "logo": "/known-logos/router-mark.svg",
        "blurb": (
            "Uniswap V2 Router02 on Ethereum (identity). "
            "Infrastructure users approve to swap/add liquidity. Not a meme token; explain router semantics accurately."
        ),
    },
    "0xe592427a0aece92de3edee1f18e0157c05861564": {
        "name": "Uniswap V3 SwapRouter",
        "symbol": None,
        "chain": "eth",
        "kind": "router",
        "logo": "/known-logos/router-mark.svg",
        "blurb": (
            "Uniswap V3 SwapRouter on Ethereum (identity). "
            "Exact-input/output swap infrastructure — not a holdable retail token contract."
        ),
    },
    "0x1f98431c8ad98523631ae4a59f267346ea31f984": {
        "name": "Uniswap V3 Factory",
        "symbol": None,
        "chain": "eth",
        "kind": "factory",
        "logo": "/known-logos/factory-mark.svg",
        "blurb": (
            "Uniswap V3 Factory on Ethereum (identity). "
            "Creates and tracks V3 pools; core DEX infrastructure, not a user-held token."
        ),
    },
    "0xca11bde05977b3631167028862be2a173976ca11": {
        "name": "Multicall3",
        "symbol": None,
        "chain": "eth",
        "kind": "infra",
        "logo": "/known-logos/multicall-mark.svg",
        "blurb": (
            "Multicall3 (canonical multi-chain helper) (identity). "
            "Batches eth_calls / aggregate3 for wallets and dapps. Not a token and not a vault of user funds."
        ),
    },
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/wbtc-mark.svg",
        "blurb": (
            "Wrapped Bitcoin (WBTC) on Ethereum mainnet (identity). "
            "ERC-20 representation of BTC custodied off-chain; not a DEX router and not a protocol treasury."
        ),
    },
    "0x514910771af9ca656af840dff83e8264ecf986ca": {
        "name": "ChainLink Token",
        "symbol": "LINK",
        "chain": "eth",
        "kind": "erc20",
        "logo": "/known-logos/link-mark.svg",
        "blurb": (
            "Chainlink LINK token on Ethereum mainnet (identity). "
            "ERC-20 with transferAndCall helper — not a DEX router and not a treasury."
        ),
    },
    "0xfb15f90298e4ccd7106e76ffb5e520315cc42b0b": {
        "name": "Lightchain AIVM JobRegistry",
        "symbol": None,
        "chain": "lightchain",
        "kind": "infra",
        "logo": "/known-logos/aivm-mark.svg",
        "blurb": (
            "Lightchain AIVM JobRegistry proxy on native mainnet (identity). "
            "Infrastructure for paid decentralized inference jobs — not a retail token."
        ),
    },
}


def lookup_known(address: str):
    if not address:
        return None
    return KNOWN_CONTRACTS.get(address.lower().strip())


# ── Async job store (for /api/analyze/start + /api/job/:id) ─────────────
_jobs: dict = {}        # job_id → {status, result, error, ts}
_jobs_lock  = threading.Lock()

def _cleanup_old_jobs():
    """Drop jobs older than 30 minutes to avoid memory leak."""
    cutoff = time.time() - 1800
    with _jobs_lock:
        stale = [k for k, v in _jobs.items() if v.get("ts", 0) < cutoff]
        for k in stale:
            del _jobs[k]


def run_inference_aivm(prompt: str, timeout: int = 300) -> str:
    """
    Run inference on Lightchain AIVM only (no Ollama in production).
    """
    client = get_aivm_client()
    if not client:
        raise RuntimeError(
            "AIVM not configured — set LIGHTCHAIN_PRIVATE_KEY on the server "
            "(wallet needs native LCAI on Lightchain mainnet)."
        )
    return client.run_inference(prompt, timeout_secs=timeout)


# Back-compat alias used by older call sites
def run_inference_aivm_or_ollama(prompt: str, timeout: int = 300) -> str:
    if AIVM_ONLY:
        return run_inference_aivm(prompt, timeout=timeout)
    # Dev-only fallback when AIVM_ONLY=0
    client = get_aivm_client()
    if client:
        try:
            return client.run_inference(prompt, timeout_secs=timeout)
        except Exception as e:
            print(f"  [AIVM] failed, Ollama fallback: {e}")
    payload = json.dumps({"model": "llama3-8b", "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        'http://localhost:11434/api/generate',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get('response', '')


# ════════════════════════════════════════════════════════════════════════
# HTTP SERVER + abuse guards (open-endpoints audit 2026-08-22)
# ════════════════════════════════════════════════════════════════════════
from datetime import datetime, timezone as _tz
_CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://smartcontractexplainer.xyz,https://keiko-dev-lcai.github.io,http://localhost:8080"
).split(",") if o.strip()]
_CHAT_RATE_PER_MIN = int(os.environ.get("CHAT_RATE_PER_MIN", "5"))
_CHAT_RATE_PER_DAY = int(os.environ.get("CHAT_RATE_PER_DAY", "30"))
_DAILY_LCAI_CAP = float(os.environ.get("DAILY_LCAI_CAP", "50"))
_LCAI_PER_JOB = float(os.environ.get("LCAI_PER_JOB", "0.02"))
_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", "8"))
_rate_lock = threading.Lock(); _rate_hits = {}
_spend_lock = threading.Lock(); _spend_day = ""; _spend_jobs = 0
_active = 0; _active_lock = threading.Lock()

def _handler_ip(h):
    xff = (h.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (h.client_address[0] if h.client_address else "unknown")

def _cors_for(h):
    origin = (h.headers.get("Origin") or "").strip()
    if origin in _CORS_ORIGINS: return origin
    return _CORS_ORIGINS[0] if _CORS_ORIGINS else "https://smartcontractexplainer.xyz"

def _gate_ai(h):
    global _spend_day, _spend_jobs, _active
    ip = _handler_ip(h); now = time.time(); day = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    with _rate_lock:
        rec = _rate_hits.get(ip)
        if not rec or rec.get("day") != day:
            rec = {"day": day, "hits": []}; _rate_hits[ip] = rec
        hits = [t for t in rec["hits"] if now - t < 86400]
        if len([t for t in hits if now - t < 60]) >= _CHAT_RATE_PER_MIN:
            return False, 429, "Too many requests — wait a minute and try again."
        if len(hits) >= _CHAT_RATE_PER_DAY:
            return False, 429, "Daily limit reached — try again tomorrow."
        hits.append(now); rec["hits"] = hits
    with _active_lock:
        if _active >= _MAX_CONCURRENT:
            return False, 503, "AI is busy right now — give it a moment and try again."
        _active += 1
    with _spend_lock:
        if _spend_day != day: _spend_day = day; _spend_jobs = 0
        if _spend_jobs * _LCAI_PER_JOB >= _DAILY_LCAI_CAP:
            with _active_lock: _active = max(0, _active - 1)
            return False, 503, "AI is at capacity for today — please try again tomorrow."
        _spend_jobs += 1
    return True, 200, ""

def _ungate_ai():
    global _active
    with _active_lock: _active = max(0, _active - 1)

class Handler(BaseHTTPRequestHandler):

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', _cors_for(self))
        self.send_header('Vary', 'Origin')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', _cors_for(self))
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Vary', 'Origin')
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        # ── API: illustrations disabled (wrong AI art was worse than none) ─
        if parsed.path == '/api/illustrate':
            self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
            self.send_json(410, {
                'error': 'Illustrations disabled',
                'disabled': True,
                'message': 'AI-generated contract art removed. Known tokens show the official mark only.',
            })
            return

        # ── API: compare two contracts ────────────────────────────────────
        if parsed.path == '/api/compare':
            ok, code, err = _gate_ai(self)
            if not ok:
                self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
                self.send_json(code, {'error': err})
                return
            try:
                self._handle_compare()
            finally:
                _ungate_ai()
            return

        # ── API: async analyze (returns job_id immediately; poll /api/job/:id) ──
        if parsed.path == '/api/analyze/start':
            ok, code, err = _gate_ai(self)
            if not ok:
                self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
                self.send_json(code, {'error': err})
                return
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            try:
                import json as _json
                req_data = _json.loads(body)
                prompt   = req_data.get('prompt', '')
                wallet   = req_data.get('wallet', '')
                paid_hint = bool(req_data.get('paid', False))
            except Exception:
                self.send_json(400, {'error': 'Invalid JSON'})
                return

            # All tiers use AIVM (production). paid_hint only for logging/access product.
            if wallet and paid_hint:
                paid = _verify_paid_access(wallet)
                print(f'  [route] AIVM · wallet={wallet[:10]}… on-chain paid={paid}')
            else:
                print(f'  [route] AIVM · free/unauthenticated tier')

            job_id = str(_uuid_mod.uuid4())
            with _jobs_lock:
                _jobs[job_id] = {
                    'status': 'pending', 'result': None,
                    'error': None, 'ts': time.time()
                }

            def _run_job(jid=job_id, p=prompt):
                try:
                    result = run_inference_aivm(p, timeout=180)
                    with _jobs_lock:
                        _jobs[jid]['status'] = 'done'
                        _jobs[jid]['result'] = result
                except Exception as exc:
                    with _jobs_lock:
                        _jobs[jid]['status'] = 'error'
                        _jobs[jid]['error']  = str(exc)

            def _run_job_wrapped(jid=job_id, p=prompt):
                try:
                    _run_job(jid, p)
                finally:
                    _ungate_ai()
            threading.Thread(target=_run_job_wrapped, daemon=True).start()
            self.send_json(200, {'mode': 'async', 'job_id': job_id, 'engine': 'aivm'})
            return

        # ── API: analyze a single contract via AI ─────────────────────────
        if parsed.path == '/api/analyze':
            ok, code, err = _gate_ai(self)
            if not ok:
                self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
                self.send_json(code, {'error': err})
                return
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            try:
                import json as _json
                req_data = _json.loads(body)
                prompt   = req_data.get('prompt', '')
                text = run_inference_aivm(prompt, timeout=180)
                self.send_json(200, {'response': text, 'done': True, 'engine': 'aivm'})
            except Exception as e:
                self.send_json(502, {'error': str(e)})
            finally:
                _ungate_ai()
            return

        self.send_response(404)
        self.end_headers()

    def _handle_compare(self):
        import json as _json
        body_raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body     = _json.loads(body_raw) if body_raw else {}
        addr_a   = body.get('address_a', '').strip()
        addr_b   = body.get('address_b', '').strip()
        chain_a  = body.get('chain_a', 'eth').strip()
        chain_b  = body.get('chain_b', 'eth').strip()

        if not addr_a or not addr_b:
            self.send_json(400, {'error': 'Both addresses required'})
            return

        def _fetch_src(address, chain):
            chain_id = CHAIN_IDS.get(chain)
            if chain_id is None:
                url = (f"{LIGHTCHAIN_API}?module=contract&action=getsourcecode"
                       f"&address={address}")
            else:
                url = (f"{ETHERSCAN_V2}?chainid={chain_id}"
                       f"&module=contract&action=getsourcecode"
                       f"&address={address}&apikey={ETHERSCAN_KEY}")
            req = urllib.request.Request(url,
                headers={"User-Agent": "SmartContractExplainer/1.0",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _json.loads(r.read())
            if data.get('status') != '1':
                raise ValueError(f"Could not fetch {address}: {data.get('message','')}")
            result = data['result'][0]
            src  = result.get('SourceCode', '') or ''
            name = result.get('ContractName', address[:8])
            return src[:6000], name   # truncate to fit in prompt

        try:
            src_a, name_a = _fetch_src(addr_a, chain_a)
            src_b, name_b = _fetch_src(addr_b, chain_b)
        except Exception as e:
            self.send_json(502, {'error': str(e)})
            return

        compare_prompt = f"""You are a smart contract teacher comparing two contracts for everyday users.
Be neutral, specific to THIS source, and never invent functions.

Silently check both for: proxy/upgradeability, mint, pause/blacklist, fees/tax, owner/admin roles,
external calls, and whether each is a token vs router/factory/infra.

Contract A: {name_a} ({addr_a[:10]}… on {chain_a})
```solidity
{src_a}
```

Contract B: {name_b} ({addr_b[:10]}… on {chain_b})
```solidity
{src_b}
```

Compare these two contracts. Structure your response as:

**Purpose**
* Contract A: one sentence on what it does.
* Contract B: one sentence on what it does.

**Similarities**
Key things they have in common (from code).

**Differences**
Key functional and structural differences (from code).

**Risk Assessment**
Rate each LOW/MEDIUM/HIGH from the source only (famous name does not force LOW).
Which is riskier for a typical user interaction and why — cite concrete privileges or patterns.

**Recommendation**
Which is safer for a careful DeFi interaction, and why. No FUD adjectives.

Token owner controls ≠ protocol DAO treasury unless the code clearly is the treasury.
Infrastructure (routers/factories/multicall) is different from holdable tokens — say so clearly."""

        try:
            text = run_inference_aivm(compare_prompt, timeout=180)
            self.send_json(200, {
                'response': text,
                'done': True,
                'name_a': name_a,
                'name_b': name_b,
                'engine': 'aivm',
            })
        except Exception as e:
            self.send_json(502, {'error': str(e)})
        return


    def do_GET(self):
        import re as _re
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # ── API: poll async job status ──────────────────────────────────
        m = _re.match(
            r'^/api/job/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
            parsed.path
        )
        if m:
            _cleanup_old_jobs()
            job_id = m.group(1)
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                self.send_json(404, {'error': 'Job not found or expired'})
                return
            if job['status'] == 'pending':
                self.send_json(200, {'status': 'pending'})
            elif job['status'] == 'done':
                self.send_json(200, {
                    'status': 'done', 'response': job['result'], 'done': True
                })
            else:
                self.send_json(200, {
                    'status': 'error', 'error': job.get('error', 'Unknown error')
                })
            return

        # ── API: fetch contract source from block explorer ──
        if parsed.path == '/api/contract':
            address = params.get('address', [''])[0].strip()
            chain   = params.get('chain',   ['auto'])[0].strip()

            if not address or len(address) != 42 or not address.startswith('0x'):
                self.send_json(400, {'status': '0', 'message': 'Invalid address'})
                return

            # Build list of chains to try
            if chain == 'auto':
                chains_to_try = list(CHAIN_IDS.keys())  # eth, bsc, polygon, arbitrum, lightchain
            else:
                chains_to_try = [chain]

            def fetch_source(c):
                cid = CHAIN_IDS.get(c)
                if cid is None:
                    url = (f"{LIGHTCHAIN_API}?module=contract&action=getsourcecode"
                           f"&address={address}")
                else:
                    url = (f"{ETHERSCAN_V2}?chainid={cid}"
                           f"&module=contract&action=getsourcecode"
                           f"&address={address}&apikey={ETHERSCAN_KEY}")
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 SmartContractExplainer/1.0',
                    'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=12) as r:
                    return json.loads(r.read()), c

            last_err = 'Contract not found or source not verified on any supported chain.'
            for c in chains_to_try:
                try:
                    data, found_chain = fetch_source(c)
                    result = data.get('result', [])
                    if (data.get('status') == '1' and isinstance(result, list)
                            and result and result[0].get('SourceCode', '')):
                        # Inject which chain it was found on
                        data['_chain'] = found_chain
                        raw = json.dumps(data).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', _cors_for(self))
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                except Exception as e:
                    last_err = str(e)
                    continue

            self.send_json(404, {'status': '0', 'message': last_err})
            return

        # ── API: health / engine status ────────────────────────────────
        if parsed.path == '/api/health':
            client = get_aivm_client()
            ready = client is not None
            self.send_json(200, {
                'status': 'ok' if ready else 'degraded',
                'service': 'SmartContractExplainer',
                'engine': 'aivm' if ready else 'none',
                'aivm_ready': ready,
                'aivm_only': AIVM_ONLY,
                'illustrations': False,
            })
            return

        # ── API: known contract metadata (logo + accurate blurb) ───────
        if parsed.path == '/api/known':
            address = params.get('address', [''])[0].strip()
            info = lookup_known(address)
            if not info:
                self.send_json(404, {'known': False})
                return
            self.send_json(200, {'known': True, **info, 'address': address.lower()})
            return

        # ── API: LCAI price in USD ──────────────────────────────────────
        if parsed.path == '/api/lcai-price':
            price = _get_lcai_price_usd()
            self.send_json(200, {'price_usd': price})
            return

        # ── API: basic wallet data via RPC (avoids browser CORS) ───────
        if parsed.path == '/api/wallet':
            address = params.get('address', [''])[0].strip()
            chain   = params.get('chain',   ['eth'])[0].strip()

            if not address or len(address) != 42 or not address.startswith('0x'):
                self.send_json(400, {'error': 'Invalid address'})
                return

            rpc_urls = CHAIN_RPC.get(chain, CHAIN_RPC['eth'])

            def rpc_call(method, rpc_params):
                last_err = None
                for rpc_url in rpc_urls:
                    try:
                        payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': rpc_params}).encode()
                        req = urllib.request.Request(rpc_url, data=payload,
                            headers={'Content-Type': 'application/json', 'User-Agent': 'SmartContractExplainer/1.0'})
                        with urllib.request.urlopen(req, timeout=10) as r:
                            data = json.loads(r.read())
                        if 'error' in data:
                            raise Exception(data['error']['message'])
                        return data['result']
                    except Exception as e:
                        last_err = e
                        continue
                raise last_err

            try:
                balance  = rpc_call('eth_getBalance',          [address, 'latest'])
                tx_count = rpc_call('eth_getTransactionCount', [address, 'latest'])
                code     = rpc_call('eth_getCode',             [address, 'latest'])
                self.send_json(200, {'balance': balance, 'txCount': tx_count, 'code': code})
            except Exception as e:
                self.send_json(502, {'error': str(e)})
            return

        # ── API: accurate token balances via per-contract lookup ────────
        if parsed.path == '/api/tokens':
            address  = params.get('address', [''])[0].strip()
            chain    = params.get('chain',   ['eth'])[0].strip()

            if not address or len(address) != 42 or not address.startswith('0x'):
                self.send_json(400, {'status': '0', 'message': 'Invalid address'})
                return

            chain_id = CHAIN_IDS.get(chain)
            if chain_id is None:
                tx_url = (f"{LIGHTCHAIN_API}?module=account&action=tokentx"
                          f"&address={address}&sort=asc&offset=5000&page=1")
            else:
                tx_url = (f"{ETHERSCAN_V2}?chainid={chain_id}"
                          f"&module=account&action=tokentx"
                          f"&address={address}&sort=asc&offset=5000&page=1"
                          f"&apikey={ETHERSCAN_KEY}")

            try:
                req = urllib.request.Request(tx_url,
                    headers={'User-Agent': 'SmartContractExplainer/1.0', 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    tx_data = json.loads(r.read())

                if tx_data.get('status') != '1' or not isinstance(tx_data.get('result'), list):
                    self.send_json(200, {'status': '1', 'result': []})
                    return

                # Build unique token contract list with metadata
                seen = {}
                for tx in tx_data['result']:
                    ca = tx.get('contractAddress', '').lower()
                    if ca and ca not in seen:
                        seen[ca] = {
                            'contractAddress': ca,
                            'symbol':   tx.get('tokenSymbol', '?'),
                            'name':     tx.get('tokenName',   '?'),
                            'decimals': tx.get('tokenDecimal','18'),
                        }

                # Fetch actual current balance for each contract
                results = []
                for ca, meta in list(seen.items())[:60]:  # cap at 60 tokens
                    if chain_id is None:
                        bal_url = (f"{LIGHTCHAIN_API}?module=account&action=tokenbalance"
                                   f"&contractaddress={ca}&address={address}&tag=latest")
                    else:
                        bal_url = (f"{ETHERSCAN_V2}?chainid={chain_id}"
                                   f"&module=account&action=tokenbalance"
                                   f"&contractaddress={ca}&address={address}&tag=latest"
                                   f"&apikey={ETHERSCAN_KEY}")
                    try:
                        req2 = urllib.request.Request(bal_url,
                            headers={'User-Agent': 'SmartContractExplainer/1.0', 'Accept': 'application/json'})
                        with urllib.request.urlopen(req2, timeout=10) as r2:
                            bal_data = json.loads(r2.read())
                        balance = bal_data.get('result', '0') or '0'
                        if balance.isdigit() and int(balance) > 0:
                            results.append({**meta, 'balance': balance})
                    except Exception:
                        pass

                self.send_json(200, {'status': '1', 'result': results})

            except Exception as e:
                self.send_json(502, {'status': '0', 'message': f'Error: {e}'})
            return


        # ── API: recent transactions for a contract address ──────────────
        if parsed.path == '/api/transactions':
            address  = params.get('address', [''])[0].strip()
            chain    = params.get('chain',   ['eth'])[0].strip()
            limit    = min(int(params.get('limit', ['15'])[0]), 25)

            if not address or len(address) != 42 or not address.startswith('0x'):
                self.send_json(400, {'status': '0', 'message': 'Invalid address'})
                return

            chain_id = CHAIN_IDS.get(chain)
            if chain_id is None:
                url = (f"{LIGHTCHAIN_API}?module=account&action=txlist"
                       f"&address={address}&sort=desc&offset={limit}&page=1")
            else:
                url = (f"{ETHERSCAN_V2}?chainid={chain_id}"
                       f"&module=account&action=txlist"
                       f"&address={address}&sort=desc&offset={limit}&page=1"
                       f"&apikey={ETHERSCAN_KEY}")

            try:
                req = urllib.request.Request(url,
                    headers={'User-Agent': 'SmartContractExplainer/1.0', 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', _cors_for(self))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as e:
                self.send_json(502, {'status': '0', 'message': f'Explorer error: {e}'})
            return

        # ── API: token transfer history for a wallet (V2 API) ──────────
        if parsed.path in ('/api/tokentx',):
            address  = params.get('address', [''])[0].strip()
            chain    = params.get('chain',   ['eth'])[0].strip()

            if not address or len(address) != 42 or not address.startswith('0x'):
                self.send_json(400, {'status': '0', 'message': 'Invalid address'})
                return

            chain_id = CHAIN_IDS.get(chain)
            if chain_id is None:
                url = (f"{LIGHTCHAIN_API}?module=account&action=tokentx"
                       f"&address={address}&sort=asc&offset=5000&page=1")
            else:
                url = (f"{ETHERSCAN_V2}?chainid={chain_id}"
                       f"&module=account&action=tokentx"
                       f"&address={address}&sort=asc&offset=5000&page=1"
                       f"&apikey={ETHERSCAN_KEY}")

            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'SmartContractExplainer/1.0', 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', _cors_for(self))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as e:
                self.send_json(502, {'status': '0', 'message': f'Explorer error: {e}'})
            return

        # ── Static file serving ──
        path = parsed.path
        if path == '/' or path == '':
            path = '/contract-explainer.html'

        # Search STATIC_DIR first, then HOME (legacy host layout)
        candidates = [
            os.path.join(STATIC_DIR, path.lstrip('/')),
            os.path.join(HOME, path.lstrip('/')),
        ]
        filepath = next((p for p in candidates if os.path.isfile(p)), None)

        if filepath:
            real = os.path.realpath(filepath)
            allowed_roots = [os.path.realpath(STATIC_DIR), os.path.realpath(HOME)]
            if not any(real.startswith(root + os.sep) or real == root for root in allowed_roots):
                self.send_response(403)
                self.end_headers()
                return
            ext   = os.path.splitext(filepath)[1].lower()
            ctype = MIME.get(ext, 'application/octet-stream')
            if ext == '.svg':
                ctype = 'image/svg+xml'
            with open(filepath, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            # HTML/JS app shell must not stick stale (identity bugs after deploys)
            if ext in ('.html', '.htm', '.js'):
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
                self.send_header('Pragma', 'no-cache')
            elif ext == '.svg':
                self.send_header('Cache-Control', 'public, max-age=3600')
            else:
                self.send_header('Cache-Control', 'public, max-age=300')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not found')

    def log_message(self, fmt, *args):
        # Show only API calls, suppress static file noise
        if '/api/' in (args[0] if args else ''):
            print(f"  [{self.address_string()}] {args[0]}")


if __name__ == '__main__':
    ready = bool(os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip())
    aivm_mode = "AIVM-only (Lightchain)" if AIVM_ONLY else "AIVM with optional local fallback"
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"\n  Smart Contract Explainer server")
    print(f"  ─────────────────────────────────")
    print(f"  Local:   http://localhost:{PORT}/")
    print(f"  Static:  {STATIC_DIR}")
    print(f"  Mode:    {aivm_mode}")
    print(f"  AIVM:    {'ready' if ready else 'MISSING LIGHTCHAIN_PRIVATE_KEY'}")
    print(f"  Illus:   disabled")
    print(f"\n  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
