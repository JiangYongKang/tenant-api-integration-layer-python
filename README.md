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
| 资源版本 | 资源响应头 `X-Resource-Version`（乐观并发版本号，单调递增） |
| 端点 | `POST /api/resources`、`GET /api/resources/{id}`、`PATCH /api/resources/{id}`、`DELETE /api/resources/{id}` |
| 运维入口 | `/admin/*`，**仅本机回环可访问**，见下文“运行时运维入口” |

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

- 粒度：`(tenant_id, operation, Idempotency-Key)`，`operation` 为
  `create`/`update`/`delete`；租户间、操作间互不影响（同一幂等键用于不同操作各算各的）。
- 相同键 + 相同操作 + 相同内容：返回首次结果（创建首次 201、重放 200；
  更新/删除首次与重放状态码一致），**副作用只执行一次**。
- 相同键 + 相同操作 + 不同内容：`IDEMPOTENCY_CONFLICT(409)`，不产生第二次副作用。
- 并发同键请求经 per-key 锁收敛：全部请求拿到同一结果，恰好一次真实副作用。
- 副作用执行失败不留残留记录，调用方可安全用同键重试。

## 资源生命周期：更新 / 删除与并发控制

在原有创建、读取之上新增更新与删除，旧接口的请求/响应字段保持不变。

### 乐观并发版本

- 每个资源带一个单调递增的整数版本号，通过响应头 **`X-Resource-Version`** 对外
  （创建、查询、更新、删除的响应都会返回）。
- `PATCH /api/resources/{id}` 请求体必须携带 `expected_version`：
  - 版本匹配：更新成功，版本号 `+1`（200，响应头回传新版本）。
  - 版本过期（资源已被别人改过）：返回 **`CONCURRENT_CONFLICT(409)`**，
    响应 `details` 同时给出 `expected_version` 与 `current_version`，
    明确告诉调用方“手上的状态已过期”，需要重新拉取后重试。
  - 两个拿着同一份旧状态的并发请求：检查与写入在同一把锁内完成，
    **恰好一个成功，另一个拿到冲突**，不会互相覆盖（丢更新）。
  - 资源不存在或已删除：`NOT_FOUND(404)`。
- `CONCURRENT_CONFLICT` 与参数错误（`VALIDATION_FAILED 400`）、鉴权问题
  （`401/403`）、幂等冲突（`IDEMPOTENCY_CONFLICT 409`）是**不同的错误码**，可稳定区分。
- 可改字段：`name`（v1/v2）、`labels`（**仅 v2**）。`v1` 更新携带 `labels` 属于
  不兼容变更，返回 `INCOMPATIBLE_CHANGE(409)`，不产生写入。

```bash
# 更新（带期望版本）
curl -XPATCH .../api/resources/$ID \
  -H 'X-Api-Version: v2' -H 'Idempotency-Key: upd-1' \
  -d '{"expected_version": 1, "name": "new-name", "labels": {"env": "prod"}}'
```

### 幂等删除

- `DELETE /api/resources/{id}` 必须带 `Idempotency-Key`。
- 首次删除：`200`，响应体 `{"id", "status", "version", "deleted": true,
  "result": "deleted", "deleted_at"}`。
- **对已删除资源再次删除：同样 `200`**，`result` 为 `"already_deleted"`，
  返回同一份可解释的删除视图（tombstone），不会变成内部错误，也没有状态残留：
  删除后 `GET` 为 `404`、计数归零、再更新为 `NOT_FOUND`。
- 删除从未存在的资源：`NOT_FOUND(404)`。

### 更新 / 删除的幂等语义

- 粒度细化为 **`(tenant_id, operation, Idempotency-Key)`**，`operation` 为
  `create` / `update` / `delete`。
- 同键 + 同操作 + 同内容重复提交：返回与第一次**完全一致的结果**——相同的状态码、
  业务字段、`X-Resource-Version`（`request_id` 按既有约定每次请求唯一），副作用只一次。
  这也包括“过期版本导致的并发冲突”：冲突结果会被记录，重放得到同样的
  `CONCURRENT_CONFLICT` 与 `current_version`。
- 同键 + 不同内容：`IDEMPOTENCY_CONFLICT(409)`，不产生第二次副作用。
- **同一个幂等键用在不同操作上各算各的**：创建用过的键仍可用于更新和删除，
  互不串扰；各自的重放也只命中自己的操作。

## 运行时运维入口（仅本机）

`/admin/*` 一组端点用于本地运维，**只接受回环对端**（默认允许
`127.0.0.1` / `::1` / `testclient`，可用 `LayerConfig.admin_allowed_hosts` 调整）；
非回环访问统一 `PERMISSION_DENIED(403)`，业务接口不受该限制。所有变更无需重启，
对后续请求立即生效，且整体原子生效，不存在改一半的状态。

| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /admin/credentials/issue` | 签发租户凭证（`tenant_id`、`scopes`、可选 `expires_at`） |
| `POST /admin/credentials/rotate` | 轮换密钥：key_id / 租户 / scope 不变，仅换密钥，旧密钥立即失效 |
| `POST /admin/credentials/revoke` | 吊销凭证；重复吊销幂等（`existed=false`，不产生新版本） |
| `PUT  /admin/rate-limits` | 按 `(tenant_id, endpoint)` 调整令牌桶 `capacity` / `refill_per_second` |
| `PUT  /admin/flags/{name}` | 设置灰度开关 `rollout`(0–100) 与租户级 `overrides` |
| `GET  /admin/config` | 当前生效配置（脱敏视图） |
| `GET  /admin/config/versions` | 有界历史中保留的版本号列表 |
| `POST /admin/config/rollback` | 回退到指定历史版本（`{"version": N}`） |

成功响应带 `X-Config-Version` 头，标明生效后的配置版本。

- **非法配置明确拒绝**：结构问题（字段缺失/类型不对）为 `VALIDATION_FAILED(400)`，
  取值非法（未知 scope、非正限流额度、rollout 越界、override 非布尔、空租户等）
  为 `INVALID_CONFIG(400)`；校验全部发生在生效之前，**正在生效的那份配置原样保留**。
- 轮换未知 key：`NOT_FOUND(404)`。

### 脱敏约定（无完整密钥外泄）

- 凭证在服务端只存 SHA-256 哈希；完整明文密钥**只在本地进程内的签发调用返回一次**，
  **不出现在任何 HTTP 响应或日志中**。
- 运维 HTTP 响应只给：`fingerprint`（哈希前 12 位，可核对）与
  `secret_masked`（形如 `abcd-****-xy` 的脱敏片段）。
- 日志只记录 `key_id` / 指纹 / 配置版本，绝不记录密钥原文。
- `GET /admin/config` 的配置视图中凭证也只含 `key_id / tenant_id / fingerprint /
  scopes / expires_at`。

## 配置版本与回退

- 每次**整体生效**的变更（凭证签发/轮换/吊销、限流调整、灰度调整、回退）都产生一个
  **单调递增**的配置版本号；配置是不可变快照（`RuntimeConfig`），
  通过单次原子指针替换生效。
- **请求级一致性**：每个请求在开始时固定一个配置快照，请求内的鉴权、限流规则、
  灰度判定都取自这同一份快照。处理途中即使发生切换或回退，也不会把该请求带进
  中间状态。
- `GET /admin/config` 可查当前生效版本；`GET /admin/config/versions` 可查可回退版本。
- `POST /admin/config/rollback {"version": N}`：原子地把配置内容切回历史版本 N，
  **以一个新的（更大的）版本号生效**；回退后对外行为与该版本当时一致
  （凭证集合、限流额度、灰度开关一并恢复）。
- 目标版本已被有界历史丢弃、或不可用时：返回
  **`CONFIG_VERSION_NOT_FOUND(404)`**，响应 `details.available_versions` 给出
  仍可回退的版本；这与“配置内容本身非法”的 `INVALID_CONFIG(400)` **明确区分**，
  且当前生效版本不受影响。
- **历史有界**：只保留最近 `LayerConfig.config_history_size` 个版本（默认 20），
  超出后丢弃最旧版本，内存占用有界，不会无限增长。
- 令牌桶计数属于瞬时运行状态，不属于配置快照；规则身份变化（含回退）时对应桶重置，
  不会把按旧速率累积的令牌带入新配置。

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

校验失败、鉴权失败（缺失/无效/过期/权限不足）、限流、幂等冲突、乐观并发冲突、
非法配置、配置版本不存在、内部异常分别对应不同 `code` 与 HTTP 状态码
（见 `errors.py::HTTP_STATUS`）。其中：

- `CONCURRENT_CONFLICT(409)`：资源乐观版本过期（手上状态已旧），区别于
  同 409 的 `IDEMPOTENCY_CONFLICT`（幂等键被不同内容复用）。
- `INVALID_CONFIG(400)`：提交的配置内容非法；当前生效配置保留。
- `CONFIG_VERSION_NOT_FOUND(404)`：回退目标版本不存在/已被有界历史丢弃。

内部异常对外统一为 `INTERNAL_ERROR(500)`，**不泄露堆栈、内部路径或底层异常原文**。

## 请求体上限

- `LayerConfig.max_body_bytes`（默认 64 KiB）可配置。
- 先校验 `Content-Length`，再分块读取、超限即停，内存占用有界；
  超限稳定拒绝为 `PAYLOAD_TOO_LARGE(413)`。

## 验证方法

```bash
uv run pytest tests/ -v -s
```

测试覆盖（共 60+ 用例）：

- **原有能力不回归**：凭证缺失/无效/过期、权限不足、创建幂等重放与键复用、
  并发重复提交、限流超限与租户隔离、开关快照一致性/原子替换/非法配置、
  版本兼容与拒绝路径、超限请求体、内部异常屏蔽、请求标识贯穿。
- **资源生命周期**（`tests/test_lifecycle.py`）：更新成功与版本递增、
  同版本并发更新唯一获胜且冲突可区分、四类失败分类（参数/鉴权/并发/幂等）、
  更新幂等重放字段一致且副作用一次、并发冲突重放稳定、删除幂等
  （deleted/already_deleted 视图一致、无残留、非内部错误）、删未知资源 404、
  同键跨 create/update/delete 互不串扰、v1 更新 labels 明确拒绝、v2 更新 labels 通过、
  以及 store 级 32 线程真实竞争下 CAS 唯一获胜。
- **运维入口与配置版本**（`tests/test_admin_config.py`）：凭证签发（脱敏+版本+1）、
  轮换（指纹变化、旧密钥立即失效、未知 key 404）、吊销幂等不产生新版本、
  限流热更新即时生效、灰度热更新即时生效、非法配置拒绝且保留生效版本、
  当前版本查询、版本单调递增、回退原子恢复行为（凭证/限流/灰度）、
  回退到已丢弃版本为 `CONFIG_VERSION_NOT_FOUND(404)` 并与 `INVALID_CONFIG` 区分、
  历史有界只留最近 N 个、非回环访问运维入口 403、任何响应/配置视图不含完整密钥、
  以及并发回退时在途请求不被带进中间状态。

每个用例通过 `testcase` logger 打印输入与判定依据（`-s` 可见）。

### 本地手工验证运维入口

```bash
uv run uvicorn main:app --reload

# 签发（响应只给 fingerprint 与 secret_masked）
curl -s -XPOST localhost:8000/admin/credentials/issue \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"t1","scopes":["resources:read","resources:write"]}'

# 按租户调限流 / 调灰度
curl -s -XPUT localhost:8000/admin/rate-limits -H 'content-type: application/json' \
  -d '{"tenant_id":"t1","endpoint":"GET /api/resources","capacity":2,"refill_per_second":0.5}'
curl -s -XPUT localhost:8000/admin/flags/extended-status \
  -H 'content-type: application/json' -d '{"rollout":100}'

# 查当前版本 / 历史版本 / 回退
curl -s localhost:8000/admin/config
curl -s localhost:8000/admin/config/versions
curl -s -XPOST localhost:8000/admin/config/rollback \
  -H 'content-type: application/json' -d '{"version":1}'
```
