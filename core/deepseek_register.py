"""
DeepSeek 注册模块
实现 DeepSeek 账号自动注册、PoW 挑战求解、ds2api 上传
"""

from __future__ import annotations

import base64
import json
import random
import re
import secrets
import string
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from curl_cffi import requests

try:
    from numba import jit

    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

from .logger import get_logger

logger = get_logger(__name__)

# ==========================================
# DeepSeekHashV1 PoW 算法实现
# ==========================================

# Keccak round constants
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]


def _rotl64(v: int, k: int) -> int:
    """Rotate left 64-bit"""
    return ((v << k) | (v >> (64 - k))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f23_python(s: list) -> None:
    """Keccak-f[1600] rounds 1..23 (pure Python fallback)"""
    a = list(s)

    for r in range(1, 24):
        c = [0] * 5
        for i in range(5):
            c[i] = a[i] ^ a[i + 5] ^ a[i + 10] ^ a[i + 15] ^ a[i + 20]

        d = [0] * 5
        d[0] = c[4] ^ _rotl64(c[1], 1)
        d[1] = c[0] ^ _rotl64(c[2], 1)
        d[2] = c[1] ^ _rotl64(c[3], 1)
        d[3] = c[2] ^ _rotl64(c[4], 1)
        d[4] = c[3] ^ _rotl64(c[0], 1)

        for i in range(5):
            a[i] ^= d[0]
            a[i + 5] ^= d[1]
            a[i + 10] ^= d[2]
            a[i + 15] ^= d[3]
            a[i + 20] ^= d[4]

        b = [0] * 25
        b[0] = a[0]
        b[10] = _rotl64(a[1], 1)
        b[20] = _rotl64(a[2], 62)
        b[5] = _rotl64(a[3], 28)
        b[15] = _rotl64(a[4], 27)
        b[16] = _rotl64(a[5], 36)
        b[1] = _rotl64(a[6], 44)
        b[11] = _rotl64(a[7], 6)
        b[21] = _rotl64(a[8], 55)
        b[6] = _rotl64(a[9], 20)
        b[7] = _rotl64(a[10], 3)
        b[17] = _rotl64(a[11], 10)
        b[2] = _rotl64(a[12], 43)
        b[12] = _rotl64(a[13], 25)
        b[22] = _rotl64(a[14], 39)
        b[23] = _rotl64(a[15], 41)
        b[8] = _rotl64(a[16], 45)
        b[18] = _rotl64(a[17], 15)
        b[3] = _rotl64(a[18], 21)
        b[13] = _rotl64(a[19], 8)
        b[14] = _rotl64(a[20], 18)
        b[24] = _rotl64(a[21], 2)
        b[9] = _rotl64(a[22], 61)
        b[19] = _rotl64(a[23], 56)
        b[4] = _rotl64(a[24], 14)

        a[0] = b[0] ^ ((~b[1]) & b[2])
        a[1] = b[1] ^ ((~b[2]) & b[3])
        a[2] = b[2] ^ ((~b[3]) & b[4])
        a[3] = b[3] ^ ((~b[4]) & b[0])
        a[4] = b[4] ^ ((~b[0]) & b[1])
        a[5] = b[5] ^ ((~b[6]) & b[7])
        a[6] = b[6] ^ ((~b[7]) & b[8])
        a[7] = b[7] ^ ((~b[8]) & b[9])
        a[8] = b[8] ^ ((~b[9]) & b[5])
        a[9] = b[9] ^ ((~b[5]) & b[6])
        a[10] = b[10] ^ ((~b[11]) & b[12])
        a[11] = b[11] ^ ((~b[12]) & b[13])
        a[12] = b[12] ^ ((~b[13]) & b[14])
        a[13] = b[13] ^ ((~b[14]) & b[10])
        a[14] = b[14] ^ ((~b[10]) & b[11])
        a[15] = b[15] ^ ((~b[16]) & b[17])
        a[16] = b[16] ^ ((~b[17]) & b[18])
        a[17] = b[17] ^ ((~b[18]) & b[19])
        a[18] = b[18] ^ ((~b[19]) & b[15])
        a[19] = b[19] ^ ((~b[15]) & b[16])
        a[20] = b[20] ^ ((~b[21]) & b[22])
        a[21] = b[21] ^ ((~b[22]) & b[23])
        a[22] = b[22] ^ ((~b[23]) & b[24])
        a[23] = b[23] ^ ((~b[24]) & b[20])
        a[24] = b[24] ^ ((~b[20]) & b[21])

        a[0] ^= _RC[r]

    for i in range(25):
        s[i] = a[i]


if HAS_NUMBA:
    import numpy as np

    @jit(nopython=True, cache=True)
    def _keccak_f23_numba(s: np.ndarray) -> None:
        """Keccak-f[1600] rounds 1..23 (numba JIT optimized)"""
        a = s.copy()
        rc = np.array([
            0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
            0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
            0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
            0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
            0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
            0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
        ], dtype=np.uint64)

        for r in range(1, 24):
            c = np.zeros(5, dtype=np.uint64)
            for i in range(5):
                c[i] = a[i] ^ a[i + 5] ^ a[i + 10] ^ a[i + 15] ^ a[i + 20]

            d = np.zeros(5, dtype=np.uint64)
            d[0] = c[4] ^ ((c[1] << 1) | (c[1] >> 63))
            d[1] = c[0] ^ ((c[2] << 1) | (c[2] >> 63))
            d[2] = c[1] ^ ((c[3] << 1) | (c[3] >> 63))
            d[3] = c[2] ^ ((c[4] << 1) | (c[4] >> 63))
            d[4] = c[3] ^ ((c[0] << 1) | (c[0] >> 63))

            for i in range(5):
                a[i] ^= d[0]
                a[i + 5] ^= d[1]
                a[i + 10] ^= d[2]
                a[i + 15] ^= d[3]
                a[i + 20] ^= d[4]

            b = np.zeros(25, dtype=np.uint64)
            b[0] = a[0]
            b[10] = (a[1] << 1) | (a[1] >> 63)
            b[20] = (a[2] << 62) | (a[2] >> 2)
            b[5] = (a[3] << 28) | (a[3] >> 36)
            b[15] = (a[4] << 27) | (a[4] >> 37)
            b[16] = (a[5] << 36) | (a[5] >> 28)
            b[1] = (a[6] << 44) | (a[6] >> 20)
            b[11] = (a[7] << 6) | (a[7] >> 58)
            b[21] = (a[8] << 55) | (a[8] >> 9)
            b[6] = (a[9] << 20) | (a[9] >> 44)
            b[7] = (a[10] << 3) | (a[10] >> 61)
            b[17] = (a[11] << 10) | (a[11] >> 54)
            b[2] = (a[12] << 43) | (a[12] >> 21)
            b[12] = (a[13] << 25) | (a[13] >> 39)
            b[22] = (a[14] << 39) | (a[14] >> 25)
            b[23] = (a[15] << 41) | (a[15] >> 23)
            b[8] = (a[16] << 45) | (a[16] >> 19)
            b[18] = (a[17] << 15) | (a[17] >> 49)
            b[3] = (a[18] << 21) | (a[18] >> 43)
            b[13] = (a[19] << 8) | (a[19] >> 56)
            b[14] = (a[20] << 18) | (a[20] >> 46)
            b[24] = (a[21] << 2) | (a[21] >> 62)
            b[9] = (a[22] << 61) | (a[22] >> 3)
            b[19] = (a[23] << 56) | (a[23] >> 8)
            b[4] = (a[24] << 14) | (a[24] >> 50)

            for i in range(5):
                idx = i * 5
                a[idx] = b[idx] ^ ((~b[idx + 1]) & b[idx + 2])
                a[idx + 1] = b[idx + 1] ^ ((~b[idx + 2]) & b[idx + 3])
                a[idx + 2] = b[idx + 2] ^ ((~b[idx + 3]) & b[idx + 4])
                a[idx + 3] = b[idx + 3] ^ ((~b[idx + 4]) & b[idx])
                a[idx + 4] = b[idx + 4] ^ ((~b[idx]) & b[idx + 1])

            a[0] ^= rc[r]

        for i in range(25):
            s[i] = a[i]

    _keccak_f23 = _keccak_f23_numba
else:
    _keccak_f23 = _keccak_f23_python


def deepseek_hash_v1(data: bytes) -> bytes:
    """
    DeepSeekHashV1 = SHA3-256 but skip Keccak-f[1600] round 0 (only rounds 1..23)
    rate=136, padding=0x06+0x80, output=32 bytes
    """
    RATE = 136

    if HAS_NUMBA:
        s = np.zeros(25, dtype=np.uint64)
    else:
        s = [0] * 25

    off = 0
    while off + RATE <= len(data):
        for i in range(RATE // 8):
            val = struct.unpack("<Q", data[off + i * 8: off + i * 8 + 8])[0]
            if HAS_NUMBA:
                s[i] ^= np.uint64(val)
            else:
                s[i] ^= val
        _keccak_f23(s)
        off += RATE

    final = bytearray(RATE)
    final[: len(data) - off] = data[off:]
    final[len(data) - off] = 0x06
    final[RATE - 1] |= 0x80

    for i in range(RATE // 8):
        val = struct.unpack("<Q", final[i * 8: i * 8 + 8])[0]
        if HAS_NUMBA:
            s[i] ^= np.uint64(val)
        else:
            s[i] ^= val
    _keccak_f23(s)

    out = bytearray(32)
    for i in range(4):
        if HAS_NUMBA:
            struct.pack_into("<Q", out, i * 8, int(s[i]))
        else:
            struct.pack_into("<Q", out, i * 8, s[i])

    return bytes(out)


# ==========================================
# PoW 求解
# ==========================================


def build_prefix(salt: str, expire_at: int) -> str:
    """Build prefix: "<salt>_<expire_at>_" """
    return f"{salt}_{expire_at}_"


def solve_pow(
    challenge_hex: str,
    salt: str,
    expire_at: int,
    difficulty: int,
    stop_event: Optional[threading.Event] = None,
) -> Optional[int]:
    """
    Solve PoW: find nonce in [0, difficulty) such that DeepSeekHashV1(prefix+str(nonce)) == challenge
    Returns the nonce if found, None otherwise
    """
    if len(challenge_hex) != 64:
        raise ValueError("challenge must be 64 hex chars")

    target = bytes.fromhex(challenge_hex)
    t0, t1, t2, t3 = struct.unpack("<QQQQ", target[:32])

    prefix = build_prefix(salt, expire_at).encode("utf-8")
    RATE = 136

    if HAS_NUMBA:
        base_state = np.zeros(25, dtype=np.uint64)
    else:
        base_state = [0] * 25

    off = 0
    while off + RATE <= len(prefix):
        for i in range(RATE // 8):
            val = struct.unpack("<Q", prefix[off + i * 8: off + i * 8 + 8])[0]
            if HAS_NUMBA:
                base_state[i] ^= np.uint64(val)
            else:
                base_state[i] ^= val
        _keccak_f23(base_state)
        off += RATE

    tail_len = len(prefix) - off
    tail = bytearray(RATE)
    tail[:tail_len] = prefix[off:]

    for n in range(difficulty):
        if n % 1024 == 0:
            if stop_event and stop_event.is_set():
                return None

        num_str = str(n)
        num_bytes = num_str.encode("utf-8")
        num_len = len(num_bytes)

        if HAS_NUMBA:
            s = base_state.copy()
        else:
            s = list(base_state)

        total_tail = tail_len + num_len
        if total_tail < RATE:
            buf = bytearray(RATE)
            buf[:tail_len] = tail[:tail_len]
            buf[tail_len:total_tail] = num_bytes
            buf[total_tail] = 0x06
            buf[RATE - 1] |= 0x80
            for i in range(RATE // 8):
                val = struct.unpack("<Q", buf[i * 8: i * 8 + 8])[0]
                if HAS_NUMBA:
                    s[i] ^= np.uint64(val)
                else:
                    s[i] ^= val
            _keccak_f23(s)
        else:
            buf = bytearray(RATE)
            buf[:tail_len] = tail[:tail_len]
            buf[tail_len:RATE] = num_bytes[: RATE - tail_len]
            for i in range(RATE // 8):
                val = struct.unpack("<Q", buf[i * 8: i * 8 + 8])[0]
                if HAS_NUMBA:
                    s[i] ^= np.uint64(val)
                else:
                    s[i] ^= val
            _keccak_f23(s)

            buf2 = bytearray(RATE)
            rem = total_tail - RATE
            buf2[:rem] = num_bytes[RATE - tail_len:]
            buf2[rem] = 0x06
            buf2[RATE - 1] |= 0x80
            for i in range(RATE // 8):
                val = struct.unpack("<Q", buf2[i * 8: i * 8 + 8])[0]
                if HAS_NUMBA:
                    s[i] ^= np.uint64(val)
                else:
                    s[i] ^= val
            _keccak_f23(s)

        if HAS_NUMBA:
            if int(s[0]) == t0 and int(s[1]) == t1 and int(s[2]) == t2 and int(s[3]) == t3:
                return n
        else:
            if s[0] == t0 and s[1] == t1 and s[2] == t2 and s[3] == t3:
                return n

    return None


# ==========================================
# DeepSeek 注册流程
# ==========================================

DEEPSEEK_BASE = "https://chat.deepseek.com"
DEEPSEEK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


class DeepSeekChallenge:
    """PoW Challenge from DeepSeek API"""

    def __init__(
        self,
        algorithm: str,
        challenge: str,
        salt: str,
        expire_at: int,
        difficulty: int,
        signature: str,
        target_path: str,
    ):
        self.algorithm = algorithm
        self.challenge = challenge
        self.salt = salt
        self.expire_at = expire_at
        self.difficulty = difficulty
        self.signature = signature
        self.target_path = target_path

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeepSeekChallenge":
        return cls(
            algorithm=str(data.get("algorithm") or ""),
            challenge=str(data.get("challenge") or ""),
            salt=str(data.get("salt") or ""),
            expire_at=int(data.get("expire_at") or 0),
            difficulty=int(data.get("difficulty") or 0),
            signature=str(data.get("signature") or ""),
            target_path=str(data.get("target_path") or ""),
        )


def build_pow_header(challenge: DeepSeekChallenge, answer: int) -> str:
    """Build x-ds-pow-response header = base64(json({algorithm, challenge, salt, answer, signature, target_path}))"""
    payload = {
        "algorithm": challenge.algorithm,
        "challenge": challenge.challenge,
        "salt": challenge.salt,
        "answer": answer,
        "signature": challenge.signature,
        "target_path": challenge.target_path,
    }
    json_str = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(json_str.encode("utf-8")).decode("ascii")


def solve_and_build_header(
    challenge: DeepSeekChallenge,
    stop_event: Optional[threading.Event] = None,
) -> Optional[str]:
    """Solve PoW challenge and build x-ds-pow-response header"""
    if challenge.algorithm != "DeepSeekHashV1":
        raise ValueError(f"Unsupported algorithm: {challenge.algorithm}")

    difficulty = challenge.difficulty if challenge.difficulty > 0 else 144000

    answer = solve_pow(
        challenge.challenge,
        challenge.salt,
        challenge.expire_at,
        difficulty,
        stop_event=stop_event,
    )

    if answer is None:
        return None

    return build_pow_header(challenge, answer)


def _random_password(length: int = 16) -> str:
    """Generate random password"""
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(4, length) - 4))
    )
    random.shuffle(pwd)
    return "".join(pwd)


def _extract_otp_code(content: str) -> Optional[str]:
    """Extract 6-digit OTP code from email content"""
    patterns = [
        r"验证码[：:\s]*(\d{6})",
        r"code[：:\s]*(\d{6})",
        r"(\d{6})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def run_deepseek(
    proxy: Optional[str],
    emitter,
    stop_event: Optional[threading.Event] = None,
    mail_provider=None,
    proxy_pool_config: Optional[Dict[str, Any]] = None,
    ds2api_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    DeepSeek 注册主流程

    Args:
        proxy: 静态代理地址，如 "http://127.0.0.1:7890"
        emitter: EventEmitter 实例，用于日志输出
        stop_event: 可选的停止事件，用于中断注册流程
        mail_provider: 可选的邮箱提供商实例，默认使用 Mail.tm
        proxy_pool_config: 可选的动态代理池配置
        ds2api_config: 可选的 ds2api 上传配置，格式如下：
            {
                "enabled": True,
                "url": "http://your-ds2api-server:port",
                "admin_key": "your-admin-key"
            }

    Returns:
        成功时返回字典 {"email": str, "password": str, "token": str, "uploaded": bool}
        失败时返回 None

    Note:
        - 注册成功后账号信息会自动保存到 data/deepseek_accounts.json
        - 如果 ds2api 上传失败，账号会保存到 data/deepseek_failed_uploads.json
    """
    from .register import (
        EventEmitter,
        _normalize_proxy_value,
        _to_proxies_dict,
        _get_request_proxy_pool_state,
        _call_with_http_fallback,
    )

    static_proxy = _normalize_proxy_value(proxy)
    static_proxies = _to_proxies_dict(static_proxy)

    pool_cfg_raw = proxy_pool_config or {}
    pool_cfg = {
        "enabled": bool(pool_cfg_raw.get("enabled", False)),
        "api_url": str(pool_cfg_raw.get("api_url") or "").strip(),
        "auth_mode": str(pool_cfg_raw.get("auth_mode") or "query").strip().lower(),
        "api_key": str(pool_cfg_raw.get("api_key") or "").strip(),
        "count": pool_cfg_raw.get("count", 1),
        "country": str(pool_cfg_raw.get("country") or "US").strip().upper(),
        "timeout_seconds": int(pool_cfg_raw.get("timeout_seconds") or 10),
        "fetch_retries": int(pool_cfg_raw.get("fetch_retries") or 3),
        "bad_ttl_seconds": int(pool_cfg_raw.get("bad_ttl_seconds") or 180),
        "tcp_check_enabled": bool(pool_cfg_raw.get("tcp_check_enabled", True)),
        "tcp_check_timeout_seconds": float(pool_cfg_raw.get("tcp_check_timeout_seconds") or 1.2),
        "prefer_stable_proxy": bool(pool_cfg_raw.get("prefer_stable_proxy", True)),
        "stable_proxy": _normalize_proxy_value(pool_cfg_raw.get("stable_proxy") or ""),
    }

    proxy_runtime = _get_request_proxy_pool_state(pool_cfg, fallback_proxy=static_proxy)

    def _next_proxy_value() -> str:
        if pool_cfg["enabled"]:
            try:
                selected = proxy_runtime.next_proxy()
                return selected
            except Exception:
                pass
            if static_proxy:
                return static_proxy
            return ""
        return static_proxy

    def _next_proxies() -> Any:
        proxy_value = _next_proxy_value()
        return _to_proxies_dict(proxy_value)

    mail_proxy_selector = _next_proxy_value if pool_cfg["enabled"] else None
    mail_proxies_selector = _next_proxies if pool_cfg["enabled"] else None

    try:
        # ------- Step 1: Create temporary email -------
        if mail_provider is not None:
            emitter.info("Creating temporary email...", step="create_email")
            try:
                email, dev_token = mail_provider.create_mailbox(
                    proxy=static_proxy,
                    proxy_selector=mail_proxy_selector,
                    stop_event=stop_event,
                )
            except TypeError:
                email, dev_token = mail_provider.create_mailbox(proxy=static_proxy)
        else:
            from .register import get_email_and_token

            emitter.info("Creating Mail.tm temporary email...", step="create_email")
            email, dev_token = get_email_and_token(
                static_proxies,
                emitter,
                proxy_selector=mail_proxies_selector,
            )

        if not email or not dev_token:
            emitter.error("Failed to create temporary email", step="create_email")
            return None

        emitter.success(f"Temporary email created: {email}", step="create_email")

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 2: Get PoW challenge -------
        emitter.info("Fetching DeepSeek PoW challenge...", step="pow_challenge")

        session = requests.Session(impersonate="chrome")
        proxy_value = _next_proxy_value()
        proxies_dict = _to_proxies_dict(proxy_value)

        try:
            challenge_resp = _call_with_http_fallback(
                session.post,
                f"{DEEPSEEK_BASE}/api/v0/users/create_guest_challenge",
                proxies=proxies_dict,
                headers={
                    "User-Agent": DEEPSEEK_USER_AGENT,
                    "Content-Type": "application/json",
                },
                json={"target_path": "/api/v0/users/create_email_verification_code"},
                timeout=20,
            )

            if challenge_resp.status_code != 200:
                emitter.error(
                    f"Failed to get PoW challenge: HTTP {challenge_resp.status_code}",
                    step="pow_challenge",
                )
                return None

            challenge_data = challenge_resp.json().get("biz_data", {}).get("challenge", {})
            if not challenge_data:
                emitter.error("Invalid PoW challenge response", step="pow_challenge")
                return None

            challenge = DeepSeekChallenge.from_dict(challenge_data)
            emitter.success(
                f"PoW challenge received: difficulty={challenge.difficulty}, expire_at={challenge.expire_at}",
                step="pow_challenge",
            )

        except Exception as e:
            emitter.error(f"Failed to fetch PoW challenge: {e}", step="pow_challenge")
            return None

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 3: Solve PoW -------
        emitter.info("Solving PoW challenge...", step="pow_solve")

        pow_header = solve_and_build_header(challenge, stop_event=stop_event)

        if not pow_header:
            emitter.error("Failed to solve PoW challenge", step="pow_solve")
            return None

        emitter.success("PoW challenge solved", step="pow_solve")

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 4: Send verification code -------
        emitter.info("Sending verification code...", step="send_otp")

        try:
            send_otp_resp = _call_with_http_fallback(
                session.post,
                f"{DEEPSEEK_BASE}/api/v0/users/create_email_verification_code",
                proxies=proxies_dict,
                headers={
                    "User-Agent": DEEPSEEK_USER_AGENT,
                    "Content-Type": "application/json",
                    "x-ds-pow-response": pow_header,
                },
                json={"email": email, "scenario": "register"},
                timeout=20,
            )

            if send_otp_resp.status_code != 200:
                emitter.error(
                    f"Failed to send verification code: HTTP {send_otp_resp.status_code}",
                    step="send_otp",
                )
                return None

            emitter.success("Verification code sent", step="send_otp")

        except Exception as e:
            emitter.error(f"Failed to send verification code: {e}", step="send_otp")
            return None

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 5: Poll for OTP code -------
        emitter.info("Waiting for OTP code...", step="wait_otp")

        if mail_provider is not None:
            try:
                otp_code = mail_provider.wait_for_otp(
                    dev_token,
                    email,
                    proxy=static_proxy,
                    proxy_selector=mail_proxy_selector,
                    stop_event=stop_event,
                )
            except TypeError:
                otp_code = mail_provider.wait_for_otp(
                    dev_token,
                    email,
                    proxy=static_proxy,
                    stop_event=stop_event,
                )
        else:
            from .register import get_oai_code

            otp_code = get_oai_code(
                dev_token,
                email,
                static_proxies,
                emitter,
                stop_event,
                proxy_selector=mail_proxies_selector,
            )

        if not otp_code:
            emitter.error("Failed to receive OTP code", step="wait_otp")
            return None

        emitter.success(f"OTP code received: {otp_code}", step="wait_otp")

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 6: Get new PoW challenge for registration -------
        emitter.info("Fetching registration PoW challenge...", step="pow_challenge_2")

        try:
            reg_challenge_resp = _call_with_http_fallback(
                session.post,
                f"{DEEPSEEK_BASE}/api/v0/users/create_guest_challenge",
                proxies=proxies_dict,
                headers={
                    "User-Agent": DEEPSEEK_USER_AGENT,
                    "Content-Type": "application/json",
                },
                json={"target_path": "/api/v0/users/register"},
                timeout=20,
            )

            if reg_challenge_resp.status_code != 200:
                emitter.error(
                    f"Failed to get registration PoW challenge: HTTP {reg_challenge_resp.status_code}",
                    step="pow_challenge_2",
                )
                return None

            reg_challenge_data = reg_challenge_resp.json().get("biz_data", {}).get("challenge", {})
            if not reg_challenge_data:
                emitter.error("Invalid registration PoW challenge response", step="pow_challenge_2")
                return None

            reg_challenge = DeepSeekChallenge.from_dict(reg_challenge_data)
            emitter.success(
                f"Registration PoW challenge received: difficulty={reg_challenge.difficulty}",
                step="pow_challenge_2",
            )

        except Exception as e:
            emitter.error(f"Failed to fetch registration PoW challenge: {e}", step="pow_challenge_2")
            return None

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 7: Solve registration PoW -------
        emitter.info("Solving registration PoW challenge...", step="pow_solve_2")

        reg_pow_header = solve_and_build_header(reg_challenge, stop_event=stop_event)

        if not reg_pow_header:
            emitter.error("Failed to solve registration PoW challenge", step="pow_solve_2")
            return None

        emitter.success("Registration PoW challenge solved", step="pow_solve_2")

        if stop_event and stop_event.is_set():
            return None

        # ------- Step 8: Submit registration -------
        emitter.info("Submitting registration...", step="register")

        deepseek_password = _random_password(16)

        try:
            register_resp = _call_with_http_fallback(
                session.post,
                f"{DEEPSEEK_BASE}/api/v0/users/register",
                proxies=proxies_dict,
                headers={
                    "User-Agent": DEEPSEEK_USER_AGENT,
                    "Content-Type": "application/json",
                    "x-ds-pow-response": reg_pow_header,
                },
                json={
                    "email": email,
                    "email_verification_code": otp_code,
                    "password": deepseek_password,
                },
                timeout=20,
            )

            if register_resp.status_code not in (200, 201):
                emitter.error(
                    f"Registration failed: HTTP {register_resp.status_code}",
                    step="register",
                )
                return None

            register_data = register_resp.json()
            user_data = register_data.get("biz_data", {}).get("user", {})
            token = str(user_data.get("token") or "").strip()

            if not token:
                emitter.error("Registration succeeded but no token received", step="register")
                return None

            emitter.success(f"Registration successful! Token received: {token[:10]}...", step="register")

        except Exception as e:
            emitter.error(f"Registration failed: {e}", step="register")
            return None

        # ------- Step 9: Upload to ds2api (optional) -------
        uploaded = False
        if ds2api_config and ds2api_config.get("enabled"):
            emitter.info("Uploading account to ds2api...", step="upload")

            try:
                ds2api_url = str(ds2api_config.get("url") or "").strip()
                admin_key = str(ds2api_config.get("admin_key") or "").strip()

                if not ds2api_url or not admin_key:
                    emitter.warn("ds2api URL or admin_key not configured, skipping upload", step="upload")
                else:
                    upload_resp = _call_with_http_fallback(
                        requests.post,
                        f"{ds2api_url}/admin/import",
                        headers={
                            "Authorization": f"Bearer {admin_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "accounts": [
                                {
                                    "email": email,
                                    "password": deepseek_password,
                                    "name": f"Auto-registered {email}",
                                    "remark": f"Created by ds-register at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                                }
                            ]
                        },
                        timeout=20,
                    )

                    if upload_resp.status_code == 200:
                        upload_data = upload_resp.json()
                        imported = int(upload_data.get("imported_accounts") or 0)
                        if imported > 0:
                            uploaded = True
                            emitter.success("Account uploaded to ds2api", step="upload")
                        else:
                            emitter.warn("Account already exists in ds2api", step="upload")
                    else:
                        emitter.warn(
                            f"ds2api upload failed: HTTP {upload_resp.status_code}",
                            step="upload",
                        )

            except Exception as e:
                emitter.warn(f"ds2api upload error: {e}", step="upload")
                # Save failed upload for manual retry
                save_failed_upload(email, deepseek_password, token, reason=str(e))

        # Save account info to local file as backup
        save_registered_account(email, deepseek_password, token, uploaded=uploaded)

        try:
            session.close()
        except Exception:
            pass

        return {
            "email": email,
            "password": deepseek_password,
            "token": token,
            "uploaded": uploaded,
        }

    except Exception as e:
        emitter.error(f"Runtime error: {e}", step="runtime")
        return None


# ==========================================
# 账号信息持久化
# ==========================================


def save_registered_account(
    email: str,
    password: str,
    token: str,
    uploaded: bool = False,
    data_dir: Optional[Path] = None,
) -> None:
    """
    Save registered account info to local JSON file as backup.
    File: data/deepseek_accounts.json
    """
    from . import DATA_DIR

    base_dir = data_dir or DATA_DIR
    accounts_file = base_dir / "deepseek_accounts.json"

    try:
        accounts = []
        if accounts_file.exists():
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    accounts = json.load(f)
                    if not isinstance(accounts, list):
                        accounts = []
            except Exception:
                accounts = []

        account_entry = {
            "email": email,
            "password": password,
            "token": token,
            "uploaded": uploaded,
            "registered_at": datetime.now().isoformat(),
        }

        # Avoid duplicates
        existing_emails = {acc.get("email") for acc in accounts if isinstance(acc, dict)}
        if email not in existing_emails:
            accounts.append(account_entry)
            with open(accounts_file, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save account info: {e}")


def save_failed_upload(
    email: str,
    password: str,
    token: str,
    reason: str = "",
    data_dir: Optional[Path] = None,
) -> None:
    """
    Save account with failed ds2api upload to separate JSON for manual retry.
    File: data/deepseek_failed_uploads.json
    """
    from . import DATA_DIR

    base_dir = data_dir or DATA_DIR
    failed_file = base_dir / "deepseek_failed_uploads.json"

    try:
        failed_accounts = []
        if failed_file.exists():
            try:
                with open(failed_file, "r", encoding="utf-8") as f:
                    failed_accounts = json.load(f)
                    if not isinstance(failed_accounts, list):
                        failed_accounts = []
            except Exception:
                failed_accounts = []

        failed_entry = {
            "email": email,
            "password": password,
            "token": token,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }

        # Avoid duplicates
        existing_emails = {
            acc.get("email") for acc in failed_accounts if isinstance(acc, dict)
        }
        if email not in existing_emails:
            failed_accounts.append(failed_entry)
            with open(failed_file, "w", encoding="utf-8") as f:
                json.dump(failed_accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save failed upload info: {e}")
