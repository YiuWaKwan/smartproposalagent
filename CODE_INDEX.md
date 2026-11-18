# 代码索引：第一阶段底座

当前仅有框架底座；第一阶段业务、数据库、鉴权、前端和真实服务尚未实现。所有流程节点显式抛出 `WorkflowNotImplemented`，没有模拟业务成功。

生成或修改业务流程前，先读 [BUSINESS_CODE_INDEX.md](BUSINESS_CODE_INDEX.md)，其中索引了 Java SmartProposal API、Python Douyin Agent 服务、字段映射和已发现契约差异。

## 启动与验证

Python 3.11+：`python -m venv .venv`，然后 `.venv/bin/python -m pip install -e '.[dev]'`，启动 `.venv/bin/uvicorn app.main:app`，执行 `.venv/bin/python -m pytest -q`。`GET /health` 的 `workflow=not_implemented` 只表示进程可响应，不表示业务可用。依赖范围见 [pyproject.toml](pyproject.toml)；本次未生成生产锁文件。

## 调用链

`app.main:app` → lifespan 创建 `WorkflowService` → `build_graph()` 编译 LangGraph。后续经已鉴权的事件受理和 worker 调用 `WorkflowService.process` → 图的 `prepare_context` → 其余节点。当前没有公开业务 HTTP 接口，也没有将客户端输入直接送进图；最小测试仅在进程内调用图并验证它在首个未实现节点停止。

| 文件 | 职责及关系 |
|---|---|
| [app/main.py](app/main.py) | ASGI 入口和生命周期，装配工作流服务 |
| [app/api/routes.py](app/api/routes.py) | 当前健康接口；未来接入受鉴权的会话、事件、SSE、结果和记忆 API |
| [app/config.py](app/config.py) | 应用基本配置；真实环境配置和密钥管理待接入 |
| [app/services/workflow.py](app/services/workflow.py) | 图的应用层入口；待接持久化事件、租约和恢复 |
| [app/graphs/campaign/graph.py](app/graphs/campaign/graph.py) | 图组装和路由位置；条件路由与 checkpoint 尚未实现 |
| [app/graphs/campaign/nodes.py](app/graphs/campaign/nodes.py) | 设计 §6 各节点及具体 TODO；所有节点拒绝执行 |
| [app/graphs/campaign/state.py](app/graphs/campaign/state.py) | 可序列化图状态的初始类型，后续扩充并验证 Postgres serializer |
| [app/schemas/domain.py](app/schemas/domain.py) | 可信身份、Campaign 和任务草稿、工作流状态、选择草稿 |
| [app/schemas/events.py](app/schemas/events.py) | 消息和最终确认事件的初始类型；其他事件、白名单 patch 和入站协议待补齐 |
| [app/services/contracts.py](app/services/contracts.py) | 业务服务、会话仓库、记忆服务的初始边界；仅为待确认契约，不是实现 |
| [app/prompts/README.md](app/prompts/README.md) | 提示词和上下文组装接入位置 |
| [app/repositories/README.md](app/repositories/README.md) | PostgreSQL 仓储、迁移与事务接入位置 |
| [app/workers/README.md](app/workers/README.md) | Outbox、轮询和对账 worker 接入位置 |
| [tests/test_base.py](tests/test_base.py) | 导入、启动、图编译/阻断和核心类型最小验证 |

## 第一阶段需求映射与待实现项

| 设计需求 | 承接位置及当前状态 |
|---|---|
| 自然语言、字段 patch、歧义及来源 | `nodes.py` 的 interpret/apply/validate，`schemas/`，`prompts/`；尚未实现模型或合并校验 |
| Campaign 和选人任务创建 | `reserve_business_operation`、`services/contracts.py`；待正式身份、schema、幂等和 API 契约 |
| 推荐异步执行、轮询与候选完整性 | `run_recommendation`、`select_candidates`、`workers/`；未连接 UAT 或真实服务 |
| 候选卡、选择与最终确认 | `SelectionDraft`、`ConfirmSelection`、selection 节点；Java 已有 `batch-select`（持久化逐 KOL 圈选）和 `batch-confirm-selection`（事务内圈选并封板）。Agent 是否使用后者作为产品最终确认入口、候选全集契约仍需确认 |
| 会话/消息、恢复、SSE、租约、Outbox、幂等 | `api/`、`services/workflow.py`、`repositories/`、`workers/`；当前没有数据库或 checkpoint，图拓扑不是可恢复实现 |
| 用户品牌授权、记忆设置/撤销与来源 | `ActorContext`、`MemoryService` 边界；未实现可信身份、访问检查、记忆表、冲突或删除 |
| 工作台面板及历史、候选卡、记忆管理 | 前端源码不在本目录；待确认现有工作台仓库及集成方式，未创建伪前端 |
| 操作审计、模型上下文/预算、配置、监控和部署 | `config.py`、`prompts/`、`workers/`，后续落实设计 §§6.3、9；未声称生产就绪 |

图上等待节点、条件分支、异步恢复、最终确认等均尚未实现；目前的边仅提供节点连接位置，不代表业务路径已通过验证。下游 API、字段、版本、身份和数据保留规则以评审结果为准。memU 仅为设计中的可选 PoC，本底座未引入。
