"""多租户鉴权：本地凭证库、过期校验、权限范围（scope）校验。

约束：
- 状态只落在本地内存，不依赖外部服务或真实凭据。
- 任何返回值、异常、日志都不得包含完整凭证，只暴露指纹（hash 前缀）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

from .errors import ApiError, ErrorCode


def _fingerprint(secret: str) -> str:
    """凭证指纹：只用于日志/响应定位，不可逆且不含原文。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class TenantCredential:
    tenant_id: str
    key_id: str
    secret_hash: str  # sha256，本地仅存哈希
    scopes: frozenset[str]
    expires_at: float | None = None  # epoch 秒；None 表示不过期

    @property
    def fingerprint(self) -> str:
        return self.secret_hash[:12]


@dataclass(frozen=True)
class AuthContext:
    """一次请求的鉴权结果，贯穿整个请求生命周期。"""

    tenant_id: str
    key_id: str
    scopes: frozenset[str]
    credential_fingerprint: str


class CredentialStore:
    """线程安全的本地凭证库。

    两种工作模式：
    - 独立模式（默认）：register/revoke 直接改本地字典，行为与最初版本一致；
    - 绑定 ConfigManager 后：写操作走配置版本化提交，authenticate 始终基于
      当前生效快照，运维入口和测试/装配入口看到的是同一份凭证状态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key_id: dict[str, TenantCredential] = {}
        self._manager = None

    def bind(self, manager) -> None:
        self._manager = manager

    def register(
        self,
        tenant_id: str,
        scopes: set[str],
        expires_at: float | None = None,
        secret: str | None = None,
    ) -> tuple[str, str]:
        """注册租户凭证，返回 (key_id, secret)。secret 仅此处出现一次。"""
        if self._manager is not None:
            return self._manager.issue_credential(
                tenant_id, set(scopes), expires_at=expires_at, secret=secret
            )
        secret = secret or secrets.token_urlsafe(32)
        key_id = f"{tenant_id}.{secrets.token_hex(4)}"
        cred = TenantCredential(
            tenant_id=tenant_id,
            key_id=key_id,
            secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            scopes=frozenset(scopes),
            expires_at=expires_at,
        )
        with self._lock:
            self._by_key_id[key_id] = cred
        return key_id, secret

    def revoke(self, key_id: str) -> None:
        if self._manager is not None:
            self._manager.revoke_credential(key_id)
            return
        with self._lock:
            self._by_key_id.pop(key_id, None)

    def authenticate(
        self, key_id: str | None, secret: str | None, now: float | None = None,
        snapshot=None,
    ) -> AuthContext:
        """校验凭证，区分缺失 / 无效 / 过期三种失败。

        传入 snapshot 时基于该不可变快照判定，保证在途请求不受配置切换/回退影响。
        """
        now = time.time() if now is None else now
        if not key_id or not secret:
            raise ApiError(ErrorCode.CREDENTIAL_MISSING, "missing credential headers")
        if snapshot is not None:
            cred = snapshot.credentials.get(key_id)
        elif self._manager is not None:
            cred = self._manager.current().credentials.get(key_id)
        else:
            with self._lock:
                cred = self._by_key_id.get(key_id)
        if cred is None or not hmac.compare_digest(
            cred.secret_hash, hashlib.sha256(secret.encode("utf-8")).hexdigest()
        ):
            raise ApiError(ErrorCode.CREDENTIAL_INVALID, "invalid credential")
        if cred.expires_at is not None and now >= cred.expires_at:
            raise ApiError(ErrorCode.CREDENTIAL_EXPIRED, "credential expired")
        return AuthContext(
            tenant_id=cred.tenant_id,
            key_id=cred.key_id,
            scopes=cred.scopes,
            credential_fingerprint=cred.fingerprint,
        )


def require_scope(ctx: AuthContext, scope: str) -> None:
    """权限范围校验，不足时明确拒绝。"""
    if scope not in ctx.scopes:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            f"scope '{scope}' required",
            details={"required_scope": scope},
        )
