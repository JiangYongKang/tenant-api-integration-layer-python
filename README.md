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
| 端点 | `POST /api/resources`（创建）、`GET /api/resources/{id}`（读）、`PATCH /api/resources/{id}`（更新，v2）、`DELETE /api/resources/{id}`（删除，v2）；另有仅本机可用的 `/admin/*` 运维入口 |

## 资源生命周期（v2 新增 `resource_lifecycle` 能力）

更新与删除只在声明 `X-Api-Version: v2` 时可用；旧版本（v1）调用一律
`UNSUPPORTED_CAPABILITY(400)`，明确拒绝，不静默降级或忽略。

### 更新 `PATCH /api/resources/{id}`

- 请求体必须带整数 `expected_version`（乐观并发前提），可改 `name` / `labels`。
- 服务端当前版本与期望不一致时返回 **`CONCURRENT_CONFLICT(409)`**，
  `details` 中给出 `expected_version` 与服务端 `current_version`，
  让调用方一眼看出自己手上的状态已过期。该错误码与参数错误
  （`VALIDATION_FAILED 400`）、鉴权问题、幂等冲突（`IDEMPOTENCY_CONFLICT 409`）
  各自独立、可机读区分。
- 成功返回更新后的资源，整数 `version` 单调递增，并带 `updated_at`。
- 两个拿着同一旧版本的并发请求：恰好一个成功，另一个拿到并发冲突。

### 删除 `DELETE /api/resources/{id}`

- 可选 `If-Match: <version>`：携带时同样做乐观并发校验，过期返回
  `CONCURRENT_CONFLICT(409)` 且不删除。
- **删除幂等**：删除保留墓碑（tombstone）。重复删除（同一幂等键或新键）都得到
  一致、可解释的 `200`（`deleted: true`），不会变成内部错误；已删资源的
  `GET` 稳定返回 `NOT_FOUND(404)`，不会残留成一个活资源。

### 生命周期操作的幂等

- 更新、删除与创建一样必须携带 `Idempotency-Key`，且同样“相同键 + 相同内容
  重放：状态码与全部业务响应字段逐字一致（含 `version`、`updated_at`），
  副作用只发生一次”；`request_id` 按请求生成，不在一致性比较范围内。
- **幂等命名空间按操作隔离**：记录键为 `(tenant, operation, Idempotency-Key)`，
  `create` / `update` / `delete` 各自独立。同一个幂等键用在不同操作上互不串扰；
  同一操作内同键不同内容仍是 `IDEMPOTENCY_CONFLICT(409)`。
- 更新的内容指纹包含 `expected_version`；删除的内容指纹包含 `If-Match`。
- 新增的 `version` / `updated_at` / `deleted` 字段只在支持生命周期的 v2 响应中
  出现，v1 响应按版本投影裁剪；创建响应不含这些生命周期字段（取当前版本请用
  `GET`）。

### 创建响应字段（v1 / v2 一致的老契约）

`POST /api/resources` 成功（201）的响应体包含创建时提交与服务端生成的字段：

- v1：`id`、`name`（**原样回显创建时提交的资源名**）、`status`、`created_at`；
- v2：在 v1 基础上额外含 `labels`（未传时为 `{}`）；
- 灰度 `extended-status` 打开且版本具备该能力时，v2 响应再附加 `status_detail`；
- 两个版本的创建响应都**不含** `version` / `updated_at` / `deleted`。

同一 `Idempotency-Key` 重放时（HTTP 200），响应业务字段（含 `name`、`labels`、
`created_at`、`status_detail`）与首次**逐字一致**，且不会创建第二个资源；
`request_id` 按请求生成，不在一致性比较范围内。

### 删除后的状态与存活统计

- 删除只置墓碑：`GET` 已删资源稳定 `NOT_FOUND(404)`，资源不再被视为存活。
- **存活数量只统计未删除资源**：资源删除后不计入存活数（全部删除则归零），
  重复删除（同键重放或换新键）不会改变存活数，墓碑不留任何计数/状态残留。
- 删除从未存在过的资源同样返回稳定的 `200`（`deleted: true`），多次调用
  （含换新幂等键）结果逐字一致，不影响任何统计。

## 本机运维入口（`/admin/*`）

仅接受来自 loopback（`127.0.0.1` / `::1`）的请求；其他来源一律 `404`，
对外部不暴露入口存在。所有变更**整体原子生效**（凭证 + 限流 + 灰度在同一份
不可变配置快照内一起切换），改完无需重启即对后续请求生效；正在处理中的请求
继续使用请求开始时拿到的快照，不会被带进中间状态。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /admin/credentials` | 签发租户凭证（body：`tenant_id`、`scopes`、可选 `secret`/`expires_at`） |
| `POST /admin/credentials/{key_id}/rotate` | 轮换：换发新 key/secret 并原子吊销旧 key；旧 key 不存在 `404` |
| `POST /admin/credentials/{key_id}/revoke` | 吊销；重复吊销幂等（`existed` 区分该 key 吊销前是否存在）。吊销不存在的凭证不产生配置新版本 |
| `PUT /admin/rate-limits/{tenant_id}` | 按 `(租户, endpoint)` 调整 `capacity` / `refill_per_second` |
| `PUT /admin/flags/{name}` | 调整单个灰度开关（`rollout`、`overrides`） |
| `PUT /admin/flags` | 整体替换全部灰度开关（原子） |
| `GET /admin/config/version` | 查询当前生效配置版本与在保留范围内的历史版本 |
| `POST /admin/config/rollback` | 回退到指定历史版本（body：`{"version": n}`） |

非法配置（未知 scope、过短密钥、非正容量/速率、rollout 越界、override 非布尔、
字段类型错误等）统一 `INVALID_CONFIG(400)` 拒绝，**保留正在生效的旧配置，
配置版本不前进**。回退入参形态非法（如版本不是整数）是
`VALIDATION_FAILED(400)`，与“配置本身非法”区分。

## 配置版本、历史与回退

- 每次**实际生效**的配置变更（包括回退）都得到一个**严格单调递增**的配置版本号；
  未产生实际变更的调用（吊销不存在的凭证、被拒绝的非法配置）不产生新版本。
- 回退以目标版本的内容生成一个新的当前版本（版本号继续递增），**原子整体
  切换**，切换后对外行为与该目标版本当时一致（凭证、限流额度、灰度一并恢复）；
  回退同时清空限流令牌桶，避免桶状态残留导致行为与目标版本不符。
- 回退到一个不存在或已被淘汰的版本：**`CONFIG_VERSION_NOT_FOUND(404)`**，
  `details.available_versions` 给出当前可回退的版本；它与 `INVALID_CONFIG`
  （配置内容非法）是不同的错误码。
- 历史版本**有界保留**（默认 10，可用 `LayerConfig.config_history_limit`
  调整），超出丢弃最旧版本；当前生效版本绝不会被丢弃。
- **空操作不产生版本**：吊销一个当前并不存在的凭证（已吊销过、或从未签发）时
  没有任何实际配置变更，**配置版本号保持不变**，也不在有界历史中占用槽位——
  反复空吊销不会把真正生效过的版本顶掉而导致日后无法回退。接口仍稳定返回
  `200 {"revoked": true, "existed": false, "config_version": n}`：
  `existed=false` 表明本次没有发生实际吊销，`config_version` 停在上一次真实
  变更的版本。只有吊销一个当前存在的凭证才推进版本。同理，非法配置被拒绝时
  版本也不前进。

## 脱敏约定

- 凭证在本地仅存 SHA-256 哈希；业务侧日志只出现不可逆指纹（哈希前 12 位）。
- 运维入口**任何响应都不回显完整密钥**：只给 `secret_masked`
  （保留首 2 / 末 2 位、中间固定掩码，如 `pl****yz`，供人工核对）与
  `secret_fingerprint`（不可逆指纹）。明文 secret 只在签发/轮换的当次由调用方
  自己保存，之后不再展示。
- 运维日志仅记录 `key_id`、指纹与事件（issued/rotated/revoked/rate_limit_updated/
  flag_updated/config_rolled_back），不记录完整密钥。


## 版本兼容规则

- 多版本同时对外服务（当前 `v1`、`v2`），请求必须显式声明版本。
- **字段新增（可选字段）向后兼容**：`v2` 新增可选字段 `labels`、
  `expected_version`（更新用），`v2` 请求可携带；旧版本行为冻结。
- **能力按版本门控**：`v2` 拥有 `extended_status` 与 `resource_lifecycle`
  （更新/删除与 `version`/`updated_at`/`deleted` 字段）能力；`v1` 无这些能力。
- **语义变更不兼容**：旧版本请求携带新版本字段属于不兼容变更，返回
  `INCOMPATIBLE_CHANGE(409)`，绝不静默降级或忽略；旧版本调用新能力端点返回
  `UNSUPPORTED_CAPABILITY(400)`。
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

- 粒度：`(tenant_id, operation, Idempotency-Key)`，租户间、操作间互不影响。
  operation 为 `create` / `update` / `delete`；同一幂等键跨操作各算各的。
- 相同键 + 相同操作 + 相同内容：返回首次结果（HTTP 200 重放，首次创建为 201；
  更新/删除首次与重放均为 200），**副作用只执行一次**，响应业务字段逐字一致。
- 相同键 + 相同操作 + 不同内容：`IDEMPOTENCY_CONFLICT(409)`，不产生第二次副作用。
- 并发同键请求经 per-key 锁收敛：全部请求拿到同一结果，恰好一次真实副作用。
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
非法配置、配置版本不存在、内部异常分别对应不同 `code` 与 HTTP 状态码
（见 `errors.py::HTTP_STATUS`）。
内部异常对外统一为 `INTERNAL_ERROR(500)`，**不泄露堆栈、内部路径或底层异常原文**。

错误码新增：`CONCURRENT_CONFLICT(409)`（乐观并发过期）、
`CONFIG_VERSION_NOT_FOUND(404)`（回退目标版本不存在）。

## 请求体上限

- `LayerConfig.max_body_bytes`（默认 64 KiB）可配置。
- 先校验 `Content-Length`，再分块读取、超限即停，内存占用有界；
  超限稳定拒绝为 `PAYLOAD_TOO_LARGE(413)`。

## 验证方法

```bash
uv run pytest tests/ -v -s
```

测试覆盖：

- 原有（无回归）：凭证缺失/无效/过期、权限不足、幂等重放与键复用、并发重复提交
  （16 线程同键收敛）、并发冲突提交、限流超限与租户隔离、开关快照一致性/
  原子替换/非法配置、版本兼容与拒绝路径、超限请求体、内部异常屏蔽、
  请求标识贯穿。
- 新增生命周期（`tests/test_lifecycle.py`）：更新成功与版本递增、并发更新单胜者
  与过期版本提示、并发冲突与幂等/参数错误的错误码区分、更新幂等重放逐字一致、
  删除幂等（同键重放 / 已删再删）、过期 `If-Match` 删除冲突、并发删除收敛到同一
  墓碑、同一幂等键跨 create/update/delete 隔离、v1 使用生命周期操作被拒、缺
  `expected_version` 拒绝、版本字段按版本投影。
- 新增运维入口（`tests/test_admin.py`）：凭证签发脱敏且可用、轮换旧 key 失效/
  新 key 生效、轮换不存在 key 为 404、吊销幂等、非法 scope 拒绝且不产生新版本、
  限流运行中调整即时生效、非法限流规则拒绝、灰度单改/整体替换/非法回滚保留、
  配置版本单调递增与可查询、历史有界淘汰最旧、回退恢复对外行为（被吊销凭证重新
  有效、限流额度恢复）、回退不存在版本 404 与入参非法 400 区分、回退已淘汰版本
  404、仅 loopback 可访问运维入口、运维日志不含完整密钥。
- 配置管理单元（`tests/test_config_manager.py`）：变更全有或全无、20 并发提交无
  半状态、进行中快照不被后续提交/回退污染、回退内容独立副本、脱敏串格式。
- 本轮修复回归（`tests/test_bugfix_regression.py`）：
  - 创建响应保留 `name`（v1/v2），同幂等键重放响应（含 `name`/`labels`）逐字
    一致且只创建一个资源；
  - 吊销已吊销 / 从未签发的凭证：`existed=false`、HTTP 200，配置版本不前进，
    反复空吊销不占用有界历史槽位、真实旧版本仍可回退；真实吊销照常推进版本；
  - 删除后存活数量只统计未删除资源（归零），重复删除（同键/新键）结果一致，
    已删资源 `GET` 稳定 404，删除从未存在的资源也稳定一致。

每个用例通过 `testcase` logger 打印输入与判定依据（`-s` 可见）。

### 手动验证示例（本机）

```bash
uv run uvicorn main:app --reload

# 签发凭证（响应只有脱敏串与指纹）
curl -s -X POST localhost:8000/admin/credentials \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"t1","scopes":["resources:read","resources:write"],"secret":"supersecret123"}'

# 创建 -> GET 取 version -> 带 expected_version 更新 -> 删除（均需 v2）
curl -s -X POST localhost:8000/api/resources -H 'x-api-version: v2' \
  -H 'x-api-key-id: <key_id>' -H 'x-api-secret: supersecret123' \
  -H 'idempotency-key: c1' -H 'content-type: application/json' -d '{"name":"a"}'
# 响应含 id/name/status/created_at（v2 还含 labels）；用同一 idempotency-key
# 重放，响应业务字段与第一次逐字一致，HTTP 状态码为 200。

# 吊销幂等与配置版本：真实吊销推进一次；随后重复吊销 / 吊销不存在的 key，
# config_version 不再变化，existed=false
curl -s -X POST localhost:8000/admin/credentials/<key_id>/revoke
curl -s -X POST localhost:8000/admin/credentials/<key_id>/revoke
curl -s localhost:8000/admin/config/version

# 删除后 GET 稳定 404，重复删除（换 Idempotency-Key）响应一致
curl -s -X DELETE localhost:8000/api/resources/<rid> \
  -H 'x-api-version: v2' -H 'x-api-key-id: <key_id>' \
  -H 'x-api-secret: supersecret123' -H 'idempotency-key: d1'
curl -s localhost:8000/api/resources/<rid> -H 'x-api-version: v2' \
  -H 'x-api-key-id: <key_id>' -H 'x-api-secret: supersecret123'
# 注：存活数量（ResourceStore.count）为进程内状态，无独立 HTTP 端点，
# 通过 uv run pytest tests/test_bugfix_regression.py -v -s 验证归零语义。

# 查询 / 调整 / 回退配置版本
curl -s localhost:8000/admin/config/version
curl -s -X POST localhost:8000/admin/config/rollback \
  -H 'content-type: application/json' -d '{"version": 1}'
```
