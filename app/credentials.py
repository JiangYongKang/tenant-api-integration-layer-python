"""本地凭证签发与校验工具。

为满足"不依赖真实凭据/外部服务"，这里实现一个自包含的 Bearer 凭证：
``base64url(tenant_id).base64url(payload).base64url(HMAC-SHA256(secret, header.payload))``。
服务端配置只保存 SHA-256(完整令牌) 指纹，校验使用常数时间比较。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

# 进程启动时生成的本地密钥；不落盘、不外发。重启后旧令牌失效（本地演示语义）
_SERVER_SECRET = os.urandom(32)

SECRET_PART_LEN = 16  # 兼容旧骨架常量名


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_token(token: str) -> str:
    """对完整凭证取 SHA-256 指纹（配置里存这个，永不存原文）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sign(signing_input: bytes) -> bytes:
    return hmac.new(_SERVER_SECRET, signing_input, hashlib.sha256).digest()


@dataclass(frozen=True)
class MintResult:
    token: str
    token_hash: str


def mint_token(tenant_id: str, label: str, ttl_seconds: int | None, now: int) -> MintResult:
    """签发本地 Bearer 凭证（测试与本地引导配置使用）。"""
    expires_at = None if ttl_seconds is None else now + ttl_seconds
    payload = {"label": label, "exp": expires_at}
    header = _b64e(tenant_id.encode("utf-8"))
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header}.{body}".encode("ascii")
    sig = _b64e(_sign(signing_input))
    token = f"{header}.{body}.{sig}"
    return MintResult(token=token, token_hash=hash_token(token))


@dataclass(frozen=True)
class ParsedCredential:
    tenant_id: str
    label: str
    expires_at: int | None
    signature_b64: str


class CredentialFormatError(ValueError):
    """凭证格式无法解析。"""


def parse_token(token: str) -> ParsedCredential:
    """解析但不验证签名/租户，格式坏时抛 CredentialFormatError。"""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise CredentialFormatError("凭证必须是三段式 base64url 结构")
    header, body, sig = parts
    try:
        tenant_id = _b64d(header).decode("utf-8")
        payload = json.loads(_b64d(body).decode("utf-8"))
        _b64d(sig)
    except (ValueError, UnicodeDecodeError) as e:
        raise CredentialFormatError("凭证段无法解码") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("label"), str):
        raise CredentialFormatError("凭证载荷缺少 label")
    expires_at = payload.get("exp")
    if expires_at is not None and not isinstance(expires_at, int):
        raise CredentialFormatError("凭证过期时间必须是整数秒")
    return ParsedCredential(
        tenant_id=tenant_id,
        label=payload["label"],
        expires_at=expires_at,
        signature_b64=sig,
    )


def expected_signature_b64(token_prefix: str) -> str:
    """根据前两段（header.body）算出应有签名。"""
    return _b64e(_sign(token_prefix.encode("ascii")))


def verify_fingerprint(present: str, expected: str) -> bool:
    """常数时间比较两个指纹。"""
    return hmac.compare_digest(present.encode("ascii"), expected.encode("ascii"))
