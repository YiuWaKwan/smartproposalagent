# 代码索引：第一阶段嵌入式 Agent

当前已完成框架底座、内部子阶段 1，以及子阶段 2–4 的本地实现。子阶段 5 已有 PostgreSQL 持久化、部分事件消费、checkpoint 测试和 SSE/身份边界，但完整恢复链及真实鉴权尚未完成。selection 写入口硬关闭；真实 Java 身份、品牌授权、幂等/对账、结果全集和权威版本仍待签收。

生成或修改业务流程前，先读 [BUSINESS_CODE_INDEX.md](BUSINESS_CODE_INDEX.md)，其中索引了 Java SmartProposal API、Python Douyin Agent 服务、字段映射和已发现契约差异。
内部开发切片和审核状态见 [第一阶段内部实施子阶段TODO.md](第一阶段内部实施子阶段TODO.md)。子阶段 2、3、4 尚未完成外部契约和整体联调验收；子阶段 5 部分实现，整体未完成。

## 启动与验证

Python 3.11+：`python -m venv .venv`，然后 `.venv/bin/python -m pip install -e '.[dev]'`，启动 `.venv/bin/uvicorn app.main:app`，执行 `.venv/bin/python -m pytest -q`。`GET /health` 的 `workflow=not_implemented` 只表示进程可响应，不表示业务可用。依赖范围见 [pyproject.toml](pyproject.toml)；本次未生成生产锁文件。

## 调用链

`app.main:app` → lifespan 创建 `WorkflowService` → `build_graph()` 编译 LangGraph。后续经已鉴权的事件受理和 worker 调用 `WorkflowService.process` → 图的 `prepare_context` → 其余节点。当前没有公开业务 HTTP 接口；测试在进程内执行结构化事件、本地状态流转和 MockTransport adapter。

| 文件 | 职责及关系 |
|---|---|
| [app/main.py](app/main.py) | ASGI 入口和生命周期，装配工作流服务 |
| [app/api/routes.py](app/api/routes.py) | 当前健康接口；未来接入受鉴权的会话、事件、SSE、结果和记忆 API |
| [app/config.py](app/config.py) | 应用基本配置；真实环境配置和密钥管理待接入 |
| [app/services/workflow.py](app/services/workflow.py) | 图的应用层入口；待接持久化事件、租约和恢复 |
| [app/graphs/campaign/graph.py](app/graphs/campaign/graph.py) | LangGraph 组装，Campaign/Task operation、Recommend 和 selection review/patch/save/final 路由；checkpoint 与持久化 interrupt 尚未实现 |
| [app/graphs/campaign/nodes.py](app/graphs/campaign/nodes.py) | 确定性事件应用、确认门控、推荐轮询和结果投影；绑定当前 selection snapshot 并分流本地事件 |
| [app/graphs/campaign/state.py](app/graphs/campaign/state.py) | Pydantic 草稿、可信 actor/brand runtime context、推荐 run、preview/result 和 typed operation receipt 的图状态 |
| [app/schemas/domain/](app/schemas/domain/) | 按 actor、campaign、task、selection、workflow 和 common 拆分的核心领域模型；`AgentWorkflowStage` 与 `BusinessTaskStatus` 明确分离 |
| [app/schemas/patch.py](app/schemas/patch.py) | `set/clear/use_default` 等结构化表单 patch 类型 |
| [app/schemas/events.py](app/schemas/events.py) | 用户消息、结构化表单和业务确认；名单使用 `selection_review` / `selection_patch` / `selection_save` / `confirm_selection`；持久化 envelope / 去重仍待补齐 |
| [app/schemas/business_api.py](app/schemas/business_api.py) | Java Campaign/Task DTO、typed request/response/error/operation/unknown、trusted brand 和确认模型 |
| [app/services/adapters/smartproposal.py](app/services/adapters/smartproposal.py) | Java API camelCase 映射、canonical JSON/hash、HTTP envelope/error 分类；无自动重试，contract gate 未配置时不发送写请求 |
| [app/schemas/recommendation.py](app/schemas/recommendation.py) | Recommend/Task/results typed contract、run/poll/snapshot/recovery 模型；结果全集和 durable snapshot 能力显式保留为未确认 |
| [app/services/recommendations.py](app/services/recommendations.py) | 推荐确认、单次启动、Java 状态映射、乱序 poll 防护、有界读错误、恢复决策、候选安全投影和 snapshot 构建 |
| [app/services/recommendation_snapshots.py](app/services/recommendation_snapshots.py) | 仅进程内 volatile snapshot 引用索引；不提供 durable persistence |
| [app/schemas/selection.py](app/schemas/selection.py) | 本地候选页及 Java 单 Task payload，分开 `SelectionSaveRequest` 和 `SelectionConfirmRequest` |
| [app/schemas/domain/selection.py](app/schemas/domain/selection.py) / [app/services/selections.py](app/services/selections.py) | revision 化本地草稿、归属/快照/名单校验、独立预览/回执、恢复决策和迟到结果防护 |
| [app/services/draft_service.py](app/services/draft_service.py) | 当前第一垂直切片的确定性 patch、来源、revision、必填/日期/金额校验和新/已有 Campaign 路由准备 |
| [app/services/contracts.py](app/services/contracts.py) | typed Campaign/Task/Recommend/results/selection 业务服务 Protocol 与 volatile/durable snapshot 边界 |
| [app/services/draft_service.py](app/services/draft_service.py) | 草稿 patch/revision 校验，operation preview、用户确认、actor/revision/payload hash 检查和 Java request 构建 |
| [app/prompts/README.md](app/prompts/README.md) | 提示词和上下文组装接入位置 |
| [app/repositories/README.md](app/repositories/README.md) | PostgreSQL 仓储、迁移与事务接入位置 |
| [app/workers/README.md](app/workers/README.md) | Outbox、轮询和对账 worker 接入位置 |
| [tests/test_base.py](tests/test_base.py) | 导入、启动、图编译/阻断和核心类型最小验证 |

## 第一阶段需求映射与待实现项

| 设计需求 | 承接位置及当前状态 |
|---|---|
| 自然语言、字段 patch、歧义及来源 | `nodes.py` 的 interpret/apply/validate，`schemas/`，`prompts/`；尚未实现模型或合并校验 |
| Campaign create / 已有 Campaign 绑定 / Task create | `schemas/business_api.py`、`services/adapters/smartproposal.py`、`draft_service.py`、Campaign graph nodes；本地映射、确认和错误契约已测，真实 Java 契约/认证/幂等/对账与 UAT 未完成；写 gate 默认关闭 |
| 推荐异步执行、轮询与候选完整性 | `schemas/recommendation.py`、`services/recommendations.py`、`nodes.py`、`services/adapters/smartproposal.py`；本地状态和受限投影已实现，真实 Java/UAT、候选全集和 durable 恢复未完成 |
| Campaign/任务草稿与已有 Campaign 复用 | `app/schemas/domain/`、`app/schemas/patch.py`、`app/services/draft_service.py`、`apply_event`/`validate_and_route`；子阶段 1 已实现本地确定性闭环，未调用外部服务 |
| 候选卡、选择与最终确认 | `SelectionDraft`、selection graph 和独立 adapter gate；Agent 封板采用 `batch-confirm-selection` 语义，当前只完成不可写的本地流程 |
| 会话/消息、恢复、SSE、租约、Outbox、幂等 | `migrations/`、`app/persistence/`、`app/workers/event_worker.py`、`app/services/sse.py`；部分 PostgreSQL 集成已验证，checkpoint 与业务台账恢复尚未打通 |
| 用户品牌授权、记忆设置/撤销与来源 | runtime `ActorScope`/`TrustedBrand` 类型；尚未接入可信身份和当前用户品牌目录；记忆仍未实现 |
| 工作台面板及历史、候选卡、记忆管理 | 前端源码不在本目录；待确认现有工作台仓库及集成方式，未创建伪前端 |
| Java adapter 配置/操作审计/模型上下文/监控部署 | `config.py`、`services/adapters/smartproposal.py`、`prompts/`、`workers/`；contract flags/headers/base path 可配置且默认未验证；未声称生产就绪 |

## 子阶段 2 调用链与验证状态

本地流程：结构化事件 → `apply_event`/`draft_service` → 确定性字段和 revision 校验 → LangGraph 生成 operation preview → 当前 actor 的结构化确认事件 → 构建 typed request → adapter contract gate → Java HTTP/envelope/error 分类。Campaign 成功后生成独立 Task preview；Task 写前读取 Campaign detail 并核对可信 brand name/code。adapter 不自动重试，unknown 只能等待后续对账。

运行配置来自 `SMARTPROPOSAL_API_BASE_URL`、`SMARTPROPOSAL_API_BEARER_TOKEN`、可配置 paths 和明确签收后才可设置的身份/operation/request/idempotency/confirmation/revision/hash headers 与 contract flags。当前默认未配置，所以不会发生真实写请求。纯映射和 MockTransport contract tests 属于本地验证，不等同测试服务或真实 Java/UAT 联调。

验证：`.venv/bin/python -m pytest -q`（40 passed）；`.venv/bin/python -m compileall -q app tests`；`git diff --check`。子阶段 2 仍有 TODO 中列出的 Java identity/brand/idempotency/reconciliation 契约与测试服务/UAT 闭环阻塞。

## 子阶段 3 调用链与验证状态

本地流程：`recommendation.start_requested` → 读取并校验 Java Task → 生成独立 recommend preview/confirmation → `recommendation.confirm` → 再读 Task → 单次无请求体 recommend POST → `accepted` → `recommendation.poll` 只读 Task → `pending/in_progress/recommend_completed/failed/unknown` 映射 → 仅在 `recommend_completed` 后读取 results → 有界候选 projection 和 volatile snapshot reference。推荐启动受理不等于完成，unknown 不自动再次 POST。

`app/schemas/recommendation.py` 保留 operation/request/downstreamTaskId、snapshot hash、结果完整性和恢复决策字段；`app/services/recommendations.py` 防止旧 poll 覆盖新状态，并限制可重试读取失败次数。Java results 未证明全集/分页/稳定快照契约，因此 projection 明确不宣称候选全集完整。`app/services/recommendation_snapshots.py` 只供本地进程测试，不能替代子阶段 5 的持久化。

验证：`.venv/bin/python -m pytest -q`（40 passed）；`.venv/bin/python -m compileall -q app tests`；`git diff --check`。覆盖 bodyless recommend contract、独立双确认、受理/完成分离、unknown/no-retry、重复/乱序 poll、结果大小限制、安全投影、volatile snapshot、恢复决策和 LangGraph 分段流程。真实 recommend、Java 轮询、results 完整性、跨进程恢复和 UAT 仍未验证。

## 子阶段 4 调用链与验证状态

Recommend 终态读取 → 受限 projection / 进程内 snapshot → `selection_review` 仅翻已读取响应 → `selection_patch` 校验并更新本地 draft/revision → `selection_save` 或 `confirm_selection` 校验独立当前卡和 payload，返回对应阻断回执。两条 graph 路由不访问 Java；adapter 的独立 gate 不调用 HTTP。候选全集未确认，真实封板关闭；本地引用不是 Java 版本。

[tests/test_selections.py](tests/test_selections.py) 验证分页、参数保真、空与取消/拒绝、跨目标、重放、换快照后旧卡失效、迟到/unknown、无自动写入和两个 adapter 零 HTTP。Recommend 额外验证缺 confirmation/revision/hash header 时零请求。当前全部 66 项本地测试、compileall、diff check 通过；未做真实 Java/UAT 验收。持久化等待、异步恢复和台账留到子阶段 5。

## 子阶段 5 部分实现

- [migrations](/Users/huajun.yao/Documents/work/smart-proposal-agent/migrations) 和 [database.py](app/persistence/database.py)：版本化 PostgreSQL SQL 迁移和 psycopg 连接池；不自动迁移生产库。
- [repositories.py](app/persistence/repositories.py)：会话、消息、事件去重、表单事务、部分台账、Outbox 和租约；[snapshots.py](app/persistence/snapshots.py) 保存有界本地结果快照。
- [event_worker.py](app/workers/event_worker.py) 目前仅消费 `form_patch`；[sse.py](app/services/sse.py) 从已提交 UI 事件补拉和发送心跳；[conversations.py](app/api/conversations.py) 依赖注入可信认证和品牌授权，缺配置默认拒绝。
- `build_graph(checkpointer=...)` 可使用官方 PostgreSQL saver，[test_postgres_persistence.py](tests/test_postgres_persistence.py) 在显式配置的 PostgreSQL 测试库验证独立 interrupt/resume；业务 checkpoint 与 worker/台账恢复未整合，尚未实现完整 durable 恢复。完整 SSE cursor 过期恢复和生产鉴权也未完成。
