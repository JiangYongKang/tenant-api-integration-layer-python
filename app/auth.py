"""租户凭证认证与权限范围（scope）校验。

四类失败必须可区分：凭证缺失 / 无效 / 过期 / 权限不足。
校验顺序：存在性 -> 格式/签名/租户匹配 -> 有效期 -> （调用点）权限范围。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, TenantCredential
from app.credentials import (
    CredentialFormatError,
    expected_signature_b64,
    hash_token,
    parse_token,
    verify_fingerprint,
)
from app.errors import APIError, ErrorCode
from app.logging_setup import log_event, mask_credential

BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    label: str
    scopes: frozenset[str]
    config_version: int


def parse_bearer(header: str | None) -> str | None:
    """提取 Bearer 凭证。

    头缺失 -> None（缺失）；头存在但不是合法 Bearer 形式 -> CREDENTIAL_INVALID。
    """
    if header is None or header == "":
        return None
    if not header.startswith(BEARER_PREFIX) or len(header) <= len(BEARER_PREFIX):
        raise APIError(
            ErrorCode.CREDENTIAL_INVALID,
            401,
            "凭证格式无效：要求 Authorization: Bearer <token>",
        )
    token = header[len(BEARER_PREFIX):].strip()
    if not token or " " in token:
        raise APIError(
            ErrorCode.CREDENTIAL_INVALID,
            401,
            "凭证格式无效：Bearer 凭证不得包含空白字符",
        )
    return token


def authenticate(
    settings: Settings,
    authorization_header: str | None,
    now: int,
    logger=None,
) -> Principal:
    """校验 Authorization: Bearer ...，失败抛可区分的 APIError。"""
    token = parse_bearer(authorization_header)
    if token is None:
        raise APIError(
            ErrorCode.CREDENTIAL_MISSING,
            401,
            "缺少凭证：请提供 Authorization: Bearer <token> 头",
        )

    try:
        parsed = parse_token(token)
    except CredentialFormatError:
        if logger is not None:
            log_event(
                logger, "auth_decision",
                decision="reject", reason=ErrorCode.CREDENTIAL_INVALID.value,
                credential_fp=mask_credential(token),
            )
        raise APIError(
            ErrorCode.CREDENTIAL_INVALID, 401, "凭证格式无效或已损坏"
        ) from None

    tenant = settings.tenants.get(parsed.tenant_id)
    if tenant is None:
        raise APIError(
            ErrorCode.CREDENTIAL_INVALID, 401, "凭证无效：租户不存在或已注销"
        )

    present_hash = hash_token(token)
    matched: TenantCredential | None = None
    for cred in tenant.credentials:
        if cred.label != parsed.label:
            continue
        if verify_fingerprint(present_hash, cred.token_hash):
            matched = cred
            break

    if matched is None:
        # 签名错误、凭证被轮换/吊销都会落到这里；对调用方统一为"无效"，
        # 不区分"存在但密钥不符"以免帮助枚举。
        raise APIError(
            ErrorCode.CREDENTIAL_INVALID, 401, "凭证无效：签名不匹配或凭证已被吊销/轮换"
        )

    if matched.expires_at is not None and now >= matched.expires_at:
        raise APIError(
            ErrorCode.CREDENTIAL_EXPIRED,
            401,
            "凭证已过期，请使用未过期凭证重新获取访问权限",
            details={"expired_at": matched.expires_at},
        )

    if logger is not None:
        log_event(
            logger, "auth_decision",
            decision="allow",
            tenant_id=parsed.tenant_id,
            credential_label=parsed.label,
            credential_fp=mask_credential(token),
            scopes=sorted(matched.scopes),
        )
    return Principal(
        tenant_id=parsed.tenant_id,
        label=parsed.label,
        scopes=matched.scopes,
        config_version=settings.config_version,
    )


def require_scope(principal: Principal, required: str) -> None:
    """权限不足抛 PERMISSION_DENIED（与认证失败明确区分，状态码 403）。"""
    if required not in principal.scopes:
        raise APIError(
            ErrorCode.PERMISSION_DENIED,
            403,
            f"权限不足：该操作需要 {required} 范围",
            details={"required_scope": required, "granted_scopes": sorted(principal.scopes)},
        )
