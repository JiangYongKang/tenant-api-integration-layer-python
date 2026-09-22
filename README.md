# 多租户版本化 API 接入层（本地、无外部依赖）

面向外部集成方的接入层，统一解决：多版本 API 兼容、多租户鉴权与权限范围、
幂等写入、按租户/接口限流、灰度开关原子切换、统一机器可读错误契约，
并在并发与异常配置下给出一致且可解释的结果。

> 约束：所有状态仅保存在进程内存，不使用外部服务与真实凭据；重启即清空。

## 快速开始

```bash
uv sync --group dev
uv run uvicorn main:app --reload     # 启动（内置演示租户）
uv run pytest                        # 运行全部测试（52+ 用例）
```

接口需要 Bearer 凭证。本地生成/查看内置演示凭证：

```bash
uv run python - << 'PY'
import time, json
from app.bootstrap import build_default_settings
_, tokens = build_default_settings(int(time.time()))
print(json.dumps(tokens, indent=2, ensure_ascii=False))
PY
```

## 端到端调用示例

```bash
TOKEN="<上一步输出的 tenant-a ci-writer 明文令牌>"

# v1 创建（写请求必须带 Idempotency-Key）
curl -sS -X POST http://127.0.0.1:8000/api/v1/widgets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: order-0001" \
  -H "Content-Type: application/json" \
  -d '{"name":"hello","data":{"src":"curl"}}'

# 重复提交（同键同内容）-> 同一结果，响应头 Idempotent-Replay: true
curl -sS -D - -X POST http://127.0.0.1:8000/api/v1/widgets \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-0001" \
  -H "Content-Type: application/json" -d '{"name":"hello","data":{"src":"curl"}}'

# v2（新增可选 channel / created_at_iso）
curl -sS -X POST http://127.0.0.1:8000/api/v2/widgets \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-0002" \
  -H "Content-Type: application/json" -d '{"name":"hi","channel":"mobile"}'
```

## 文档

详细规则见 [`docs/API.md`](docs/API.md)：

- 版本兼容规则与拒绝策略（新增只做加法、旧版带新字段显式拒绝）
- 鉴权/权限模型与凭证脱敏
- 幂等语义（同键同内容收敛、同键异内容拒绝、并发、失败重试）
- 限流维度、跨租户隔离与 `Retry-After-Ms`
- 开关快照一致性与配置原子热切换（非法配置拒绝并保留旧配置）
- 两级请求体上限与内存安全
- 统一错误契约、request_id 贯穿与完整验证方法

配置文件格式见 [`docs/config.example.json`](docs/config.example.json)。

## 代码结构

```
app/
  constants.py          # 开关名/scope 白名单等稳定常量
  errors.py             # 错误码、统一错误体、全局异常处理
  logging_setup.py      # 结构化日志、request_id、凭证脱敏
  versioning.py         # 版本协商与兼容性规则
  config.py             # 不可变配置模型 + 严格校验 + 脱敏导出
  stores.py             # 配置/幂等/限流/资源 线程安全内存存储
  credentials.py        # 本地 Bearer 签发/解析/常数时间指纹校验
  auth.py               # 认证（四类可区分失败）与 scope 校验
  idempotency.py        # 幂等裁决（每键锁、占位/清理）
  ratelimit.py          # 规则解析 + 固定窗口限流
  feature_flags.py      # 请求级开关快照与安全默认
  middleware.py         # ASGI：request_id + 请求体硬上限（流式截断）
  services.py           # 版本化业务校验与响应整形
  request_context.py    # 每请求上下文
  dependencies.py       # 组合根（内存状态 + 可控时钟）
  bootstrap.py          # 本地演示配置与凭证
  main.py               # 应用工厂（错误处理/中间件/404 契约）
  routers/              # /api/v1 /api/v2 路由、/_local 管理路由
tests/                  # 按主题组织的测试（含并发、超限、热切换）
```
