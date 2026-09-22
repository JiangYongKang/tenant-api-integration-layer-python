# 版本化多租户 API 接入层说明

本服务是一个**完全本地化**的外部集成方接入层：所有状态（租户、凭证指纹、
幂等记录、限流计数、业务资源、配置）都保存在进程内存中，不依赖数据库、
缓存或任何外部服务，也不使用任何真实第三方凭据。进程重启后内存状态清空。

- 运行：`uv run uvicorn main:app --reload`
- 测试：`uv run pytest`（用例以结构化日志打印**输入**与**判定依据**，运行时 stderr 可见）
- 内置演示租户：`tenant-a / tenant-b / tenant-c / local-admin`，明文 Bearer
  凭证由 `app.bootstrap.build_default_settings()` 在进程内生成；启动时可在
  受控环境打印一次，**绝不写入配置文件、响应体或对外日志**。

---

## 1. 统一错误契约

所有错误（含 404/413/500）都返回同一机器可读结构：

```json
{
  "error": {
    "code": "rate_limited",
    "message": "超出租户在该接口的请求限额（每 1000ms 最多 3 次），请按 Retry-After-Ms 提示退避后重试",
    "request_id": "52fa9c3376f2",
    "details": { "limit": 3, "window_ms": 1000, "retry_after_ms": 980 }
  }
}
```

- `request_id` 同时出现在响应头 `X-Request-Id` 与错误体中，二者永远一致，
  并贯穿全部结构化日志。调用方可通过 `X-Request-Id` 头传入自己的追踪 ID
  （1-64 位可见 ASCII），服务端会沿用；非法值将被替换。
- 限流额外返回 `Retry-After-Ms` 响应头与 `Idempotent-Replay: true`（重放时）。

稳定错误码（可区分、不随措辞变化）：

| code | HTTP | 含义 |
|---|---|---|
| `version_not_found` | 404 | 路径/头中声明了不存在的接口版本 |
| `unsupported_capability` | 404 | 该版本/租户不具备所请求的能力（如 v2 insights 未开放） |
| `incompatible_request` | 400 | 不可兼容变更：Accept-Version 冲突、旧版本携带新版本独有字段 |
| `validation_failed` | 422/404 | 参数/字段校验失败、资源不存在 |
| `payload_too_large` | 413 | 请求体超过全局或租户上限 |
| `credential_missing` | 401 | 缺少 `Authorization: Bearer` 凭证 |
| `credential_invalid` | 401 | 凭证格式损坏/签名不符/租户不存在/已轮换吊销 |
| `credential_expired` | 401 | 凭证已过期 |
| `permission_denied` | 403 | 认证通过但缺少所需 scope |
| `idempotency_conflict` | 409 | 同一幂等键被用于不同请求内容 |
| `concurrent_conflict` | 409 | 同键前序请求尚未完成（请重试） |
| `rate_limited` | 429 | 超出租户+接口维度限额 |
| `config_invalid` | 422 | 热切换配置校验失败（旧配置继续生效） |
| `internal_error` | 500 | 内部异常：仅稳定文案 + request_id，**不含**堆栈/路径/异常原文 |

---

## 2. 接口版本与兼容规则

当前同时提供 **v1** 与 **v2**：

| 能力 | v1 | v2 |
|---|---|---|
| `POST /api/v{ver}/widgets` | ✅ | ✅ |
| `GET  /api/v{ver}/widgets/{id}` | ✅ | ✅ |
| 请求字段 `name`(必填), `data`(可选对象) | ✅ | ✅ |
| 请求字段 `channel`（可选字符串） | ❌ 仅 v2 | ✅ |
| 响应 `created_at`（Unix 秒，语义两版一致） | ✅ | ✅ |
| 响应 `created_at_iso`、`channel` | ❌ | ✅ 加法字段 |
| 响应 `metadata`（受开关控制） | ❌ | ✅ 加法字段 |
| `GET /api/v2/widgets/{id}/insights` | ❌ 能力不存在 | ✅（受开关控制） |

兼容规则是显式且可解释的：

1. **新增只做加法**：v2 新增字段全部可选，旧客户端不改一行业务即可继续工作；
   v1 的既有字段（`created_at` 等）在 v2 中保持相同类型与语义。
2. **旧版本携带新版本独有字段 → 拒绝**（`incompatible_request`，
   `details.unsupported_fields` 指明字段、`since_version` 指明起始版本）。
   选择拒绝而非静默丢弃，避免"以为生效、实际被忽略"。
3. **未知字段一律拒绝**（`validation_failed`，严格契约，不静默忽略）。
4. **版本独有能力在旧版本上显式拒绝**：`/api/v1/.../insights` 返回
   `unsupported_capability`，不做静默降级。
5. **版本协商**：版本由路径前缀决定；可选用 `Accept-Version: v2` 头声明
   期望版本，与路径不一致时返回 `incompatible_request`，避免语义歧义。
6. 访问不存在的版本（`/api/v9/...`）返回 `version_not_found`，并在
   `details.supported` 中列出支持版本。

---

## 3. 鉴权与权限模型

- 凭证形式：`Authorization: Bearer <token>`。令牌为自包含三段式
  base64url 结构 `tenant.payload.signature`，签名密钥为**进程启动时随机
  生成的本地密钥**（不落盘、不外发；重启后旧令牌失效）。
- 服务端配置只保存 `SHA-256(token)` 指纹，比较使用 `hmac.compare_digest`
  常数时间比较。任何响应、管理接口配置视图、日志都不会出现完整令牌：
  - 配置视图中指纹被脱敏为 `a1b2c3…(redacted)`；
  - 日志中凭证以 `credential_fp: "sha256:xxxxxx"`（6 位短指纹）出现，
    且 `Bearer xxx` 文本会被自动打码。
- 失败严格区分四类：
  1. `credential_missing`（无头）
  2. `credential_invalid`（非 Bearer、格式损坏、签名不符、租户未知、已吊销）
  3. `credential_expired`（`expires_at` 到期，响应含 `expired_at`）
  4. `permission_denied`（凭证有效但 scope 不足，状态码 403）
- 权限范围（scope）：
  - `widgets:read`：读取 widget / insights
  - `widgets:write`：创建 widget（写请求必须）
  - `config:read` / `config:write`：本地管理接口
- 租户隔离：读取他租户资源统一返回 404（不泄露资源存在性）；
  幂等键、限流计数均以租户为命名空间。

---

## 4. 幂等写入语义

所有 `POST /api/v{ver}/widgets` 都**必须**携带 `Idempotency-Key`
（1-200 位可见 ASCII），缺失即 `validation_failed`。

- 幂等域：`(租户ID, 幂等键)`。不同租户可使用相同字符串键而互不影响。
- 请求指纹：`SHA256(method + "\n" + path + "\n" + 原始 body 字节)`。
- **同键 + 同内容**：重复请求返回首次的 HTTP 状态码与完整响应体，
  `Idempotent-Replay: true`，业务副作用**只发生一次**。
- **同键 + 不同内容**：返回 409 `idempotency_conflict`
  （`details.reason = request_content_mismatch`），不执行任何副作用。
- **并发同键**：服务端为每个键维护专用锁，并发请求串行裁决，结果收敛一致
  （N 个并发请求：1 个真实执行，N-1 个得到重放结果）。
- **执行失败**：首个请求在写结果前抛异常时，占位记录会被清除并释放锁，
  允许用同键重试，不会"键卡死"或留下半成品状态。
- 注意：被 413/鉴权/限流等前置校验拒绝的请求**不会**占用幂等键
  （裁决发生在这些检查之后）。

---

## 5. 限流

- 维度：`(租户, endpoint_key)`，endpoint_key 形如 `POST /api/v1/widgets`。
  固定窗口计数（默认 60s 200 次，可逐租户逐接口覆盖）。
- 规则解析顺序：接口精确规则 → 租户 `default` → 全局默认。
- 超限响应：429 `rate_limited`，带 `Retry-After-Ms`（毫秒）与
  `details.limit/window_ms/retry_after_ms`；超限额的请求不计入窗口。
- **隔离性**：计数器与锁均按键独立。一个租户的突发流量不会影响其他租户，
  也不会影响同租户的其他接口；不存在跨租户计数串扰。

内置 `tenant-b` 的写接口配置为 `3 次/1000ms`，便于验证。

---

## 6. 灰度开关与配置热切换

开关（全部默认关闭，缺失即安全默认）：

- `widgets.insights`：v2 insights 能力是否对该租户开放。
- `widgets.include_metadata`：v2 创建/读取响应是否包含 `metadata` 段。

开关取值优先级：**租户显式配置 → 全局 default_flags → 安全默认(false)**。

一致性保证：

1. **请求级快照**：请求开始时构建一次不可变 `FeatureSnapshot`，整个请求
   （含多步处理）看到的开关取值恒定不变。
2. **原子热切换**：配置以不可变 `Settings` 快照整体引用替换；进行中的请求
   持有旧快照引用，新请求拿到新快照，任何请求都不会看到"半切换"状态。
3. **非法配置拒绝**：`POST /_local/config/replace` 先做完整严格校验
   （类型、范围、未知字段/scope/开关名、窗口与 body 上限区间等），
   任一项非法即返回 `config_invalid`（含 `path/reason`），**旧配置继续生效**。
4. 管理接口需要 `config:read` / `config:write` scope；`GET /_local/config`
   输出脱敏配置；`GET /_local/whoami` 可回显当前租户的开关视图与配置版本，
   便于判定"本请求到底看到了什么"。

配置文件示例见 `docs/config.example.json`。

---

## 7. 请求体与输入上限（内存安全）

两级限制：

1. **全局硬上限**（ASGI 层，认证前）：先检查 `Content-Length`，明确超限
   立即 413、**完全不读取 body**；分块传输则边收边累计，超限立即停止接收
   并返回 413（不缓冲剩余数据），杜绝大请求体无界占用内存。
2. **租户级上限**（认证后）：对已读取字节做长度比较，超出租户更小的上限
   同样稳定返回 413（`detected_via: tenant_limit`）。

字段级上限：`name ≤ 200`、`channel ≤ 64`、`data` 最多 50 个键、
单个字符串值 ≤ 4096 字符。

---

## 8. 验证方法

```bash
uv sync --group dev
uv run pytest                       # 全部用例（含并发/异常路径）
uv run pytest -k concurrent         # 仅并发收敛相关
uv run pytest -k "auth or idempo"   # 鉴权/幂等等
```

测试覆盖矩阵：

- 版本：v1/v2 并存、加法兼容、旧版带新字段、未知版本、能力不存在、
  Accept-Version 冲突；
- 鉴权：缺失/无效/篡改/未知租户/过期/权限不足，且响应与日志不回显凭证；
- 幂等：重复重放、同键异内容冲突、8 线程并发收敛、跨租户隔离、失败可重试；
- 限流：配额耗尽、跨租户/跨接口隔离、时钟推进窗口恢复、重试提示；
- 开关：开/关行为、请求级快照、原子热切换、非法配置拒绝、脱敏；
- 输入：全局/租户两级 body 上限、流式超限即停、字段长度、非法 JSON；
- 契约：错误码可区分、request_id 贯穿、内部异常不泄露堆栈/路径/原文；
- 中间件：调用方 request_id 透传/非法值替换。

每个判定用例都会在 stderr 输出结构化日志，形如：

```json
{"event":"auth_decision","decision":"allow","tenant_id":"tenant-a",
 "credential_label":"ci-writer","credential_fp":"sha256:f7d9bf",
 "scopes":["widgets:read","widgets:write"],"request_id":"..."}
{"event":"request_rejected","decision":"reject","reason":"rate_limited",
 "path":"/api/v1/widgets","input_declared_bytes":9999,"decision_limit_bytes":65536}
```
