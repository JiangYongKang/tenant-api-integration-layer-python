# 租户 API 集成层（tenant-api-integration-layer）

面向外部集成方的版本化 API 接入层。多租户鉴权、幂等写入、限流、灰度开关
在并发与异常配置下给出一致且可解释的结果。**所有状态只落在本地进程内存，
不依赖任何外部服务或真实凭据。**

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
uv run pytest tests/ -q        # 运行全部测试（用例日志打印输入与判定依据）
```

## 接口与请求约定

| 项 | 约定 |
| --- | --- |
| 版本声明 | 请求头 `X-Api-Version: v1|v2`（必填，无默认版本） |
| 鉴权 | 请求头 `X-Api-Key-Id` + `X-Api-Key-Secret` |
| 幂等 | 写请求必须携带 `Idempotency-Key` |
| 请求标识 | `X-Request-Id`（可选，缺省自动生成），贯穿响应头、响应体与日志 |
| 端点 | `POST /api/resources`（写）、`GET /api/resources/{id}`（读） |

## 版本兼容规则

- 多版本同时对外服务（当前 `v1`、`v2`），请求必须显式声明版本。
- **字段新增（可选字段）向后兼容**：`v2` 新增可选字段 `labels`，`v2` 请求可携带；
  旧版本行为冻结。
- **语义变更不兼容**：旧版本请求携带新版本字段属于不兼容变更，返回
  `INCOMPATIBLE_CHANGE(409)`，绝不静默降级或忽略。
- 未知字段一律 `VALIDATION_FAILED(400)`，不做静默忽略。
- 不存在的版本 / 未声明版本：`UNSUPPORTED_VERSION(400)`，响应附带受支持版本列表。
- 版本不支持的能力（如 `v1` 使用 `extended_status`）：`UNSUPPORTED_CAPABILITY(400)`。
- 响应按请求版本投影（字段裁剪/重命名），见 `versioning.py::project_response`。

## 鉴权与权限模型

- 凭证按租户签发，本地仅存 SHA-256 哈希；日志与响应只出现不可逆指纹（哈希前 12 位），
  **任何对外响应或日志都不回显完整凭证**。
- 失败语义严格区分：
  - 凭证缺失 → `CREDENTIAL_MISSING(401)`
  - 凭证无效（key 不存在或密钥不匹配）→ `CREDENTIAL_INVALID(401)`
  - 凭证过期 → `CREDENTIAL_EXPIRED(401)`
  - 权限范围不足 → `PERMISSION_DENIED(403)`，响应附带所需 scope
- 权限范围（scope）：`resources:read`、`resources:write`，按端点强制校验。

## 幂等语义

- 粒度：`(tenant_id, Idempotency-Key)`，租户间互不影响。
- 相同键 + 相同内容：返回首次结果（HTTP 200 重放，首次为 201），**副作用只执行一次**。
- 相同键 + 不同内容：`IDEMPOTENCY_CONFLICT(409)`，不产生第二次副作用。
- 并发同键请求经 per-key 锁收敛：全部请求拿到同一资源 id，恰好一个创建、其余重放。
- 副作用执行失败不留残留记录，调用方可安全用同键重试。

## 限流

- 维度：`(tenant_id, endpoint)` 令牌桶，租户与接口之间计数完全隔离，
  突发流量不影响其他租户，无跨租户串扰。
- 超限：`RATE_LIMITED(429)`，响应体含 `retry_after_seconds`，响应头含 `Retry-After`。
- 规则可运行中更新（`RateLimiter.set_rule`），更新即重置对应桶，避免半切换状态；
  非正容量/速率的规则被 `INVALID_CONFIG` 拒绝。
- 默认规则见 `LayerConfig.default_rate_rule`（容量 20，每秒补 10）。

## 灰度 / 开关

- 配置为不可变快照，请求开始时取一次，**同一请求内取值一致**；
  `FlagStore.replace` 原子整体替换，运行中变更不会出现半切换状态。
- 灰度按 `sha256(flag:tenant_id)` 稳定分桶（0–99），同一租户判定确定；
  租户级 `overrides` 优先于百分比。
- 非法配置（rollout 越界、override 非布尔）被拒绝并保留旧配置；
  未配置的开关安全默认为关闭。

## 统一错误契约

所有错误响应为稳定机器可读结构：

```json
{"error": {"code": "RATE_LIMITED", "message": "...", "request_id": "...", "retry_after_seconds": 1.2}}
```

校验失败、鉴权失败（缺失/无效/过期/权限不足）、限流、幂等冲突、并发冲突、
内部异常分别对应不同 `code` 与 HTTP 状态码（见 `errors.py::HTTP_STATUS`）。
内部异常对外统一为 `INTERNAL_ERROR(500)`，**不泄露堆栈、内部路径或底层异常原文**。

## 请求体上限

- `LayerConfig.max_body_bytes`（默认 64 KiB）可配置。
- 先校验 `Content-Length`，再分块读取、超限即停，内存占用有界；
  超限稳定拒绝为 `PAYLOAD_TOO_LARGE(413)`。

## 验证方法

```bash
uv run pytest tests/ -v -s
```

测试覆盖：凭证缺失/无效/过期、权限不足、幂等重放与键复用、并发重复提交
（16 线程同键收敛）、并发冲突提交、限流超限与租户隔离、开关快照一致性/
原子替换/非法配置、版本兼容与拒绝路径、超限请求体、内部异常屏蔽、
请求标识贯穿。每个用例通过 `testcase` logger 打印输入与判定依据（`-s` 可见）。
