# 第一阶段内部实施子阶段 TODO

> 本文只记录开发执行顺序，不改变《第一阶段嵌入式Agent实施计划.md》的阶段划分。每个子阶段完成后暂停等待审核；所有子阶段完成后仍需执行第一阶段整体联调和完整性检查。

## 子阶段 1：任务上下文与确定性草稿闭环 ✅

目标：区分 Agent 工作流阶段、Java 选人任务状态和 Campaign 聚合；支持新建 Campaign 与选择已有 Campaign 后创建新选人任务；完成结构化字段 patch、字段来源、revision、必填/日期/金额校验和确定性路由。

已完成：

- 拆分 `app/schemas/domain/` 下的 actor、campaign、task、selection、workflow 和 common 模型。
- 增加 `CampaignBindingMode`，明确一个 Agent workflow 可以绑定新 Campaign 或已有 Campaign；已有 Campaign 不要求重新填写 Campaign 创建字段。
- 增加 `BusinessTaskStatus`，不把它与 `AgentWorkflowStage` 混用。
- 增加 `FormPatch`、`FieldPatch`、`form_patch` 和 `campaign_selection` 事件。
- 实现确定性 patch、`set/clear/use_default`、来源记录、revision 检查和基础必填/日期/预算校验。
- LangGraph 的 `prepare_context`、结构化事件分发、patch 应用、校验和路由进入真实本地闭环；到 Java API 适配边界明确停止。

尚未完成：LLM 自然语言抽取、持久化事件去重、数据库、可信鉴权、Java API 调用、Campaign/Task operation 台账和 durable interrupt/resume。

完成标准：纯领域服务和图路由测试通过；新 Campaign 与已有 Campaign 两条本地路径可区分；不伪造外部创建成功。

## 子阶段 2：Java SmartProposal 业务适配与 Campaign/Task 创建

目标：按 `BUSINESS_CODE_INDEX.md` 实现 Java API typed adapter，建立 Campaign create、已有 Campaign 绑定、task create 的真实请求/响应映射、错误分类和 operation 幂等边界。

依赖：子阶段 1；Java 服务部署地址、认证方式、错误 envelope 和幂等策略需可配置并完成契约核对。

### 当前进度：部分完成，未通过子阶段验收

本轮完成的本地实现：

- 增加 Campaign/Task typed Java DTO、请求上下文、trusted brand、响应、错误、operation receipt、unknown 状态和确认事件模型。
- 按 Java 源码快照映射 Campaign `POST /api/campaigns`、详情 `GET /api/campaigns/{id}`、Task `POST /api/campaigns/{campaignId}/tasks`；Long ID 使用严格正整数，金额保持 Decimal，日期序列化为 ISO 日期，可选 None 在 JSON 中省略。
- 增加 canonical payload JSON/hash、operation/request/idempotency/confirmation 标识、actor scope、form revision 和 payload hash 一致性检查；本地不保存完整持久化 operation 台账。
- LangGraph 本地流程为：Campaign 草稿校验 → operation 预览 → 当前 actor 对预览的结构化确认 → adapter Campaign create → 新 Task 预览 → 独立结构化确认 → Java Campaign detail 与可信品牌核对 → Task create。已有 Campaign 选择本身不等于授权。
- Adapter 不做用户确认、身份或品牌推断，不补业务默认值，不盲目重试写请求；错误不暴露原始响应、URL、token 或堆栈。超时/连接失败/5xx/成功 envelope 缺少 ID 均产生 unknown/对账标记。
- 没有 Java 写契约 gate、认证/身份头、operation/idempotency/request/reconciliation 字段配置不完整时，adapter 在发出 HTTP 请求前拒绝写操作。
- 修正子阶段 1 相关语义：校验事件 task ID 归属；Campaign 必填与 Task 必填分阶段校验；`set null` 被拒绝，`clear` 清值并屏蔽默认，`use_default` 清掉用户覆盖并解除屏蔽但不虚构默认解析服务；`use_default` 外部默认值解析仍未实现。

本地验证：`.venv/bin/python -m pytest -q`（28 passed）、`.venv/bin/python -m compileall -q app tests`、`git diff --check` 均通过。覆盖纯映射、canonical hash、Decimal/date/null、错误 envelope、HTTP 状态、408/409/429/5xx 与超时 unknown、no-retry、缺少契约 gate 时零 HTTP 请求、Campaign→Task 双确认，以及错误品牌不得建 Task。

### 尚未完成 / 阻塞

- 仅核对了本机 Java 源码快照和前端源码；尚未核对目标部署分支、UAT 部署路径、网关路径、服务地址或通过真实接口契约签收。前端 `.env.staging` 中存在 UAT base URL，但它不是 Agent 服务端正式部署/调用地址的授权契约，本轮没有调用。
- Java 使用 Venus OAuth，审计主体由 `SecurityUtils` 提供；尚无 Agent 服务到 Java 的委托用户/tenant/brand 凭证传递协议。不能把尚未证实的自定义 header 当成 Java 已支持。
- 前端品牌候选 `list-by-current-user` 返回值被页面按 `code/name` 使用；尚未核对 Agent 后端服务端调用认证、响应 schema、brand ID/code/name 的权威映射及缓存/撤权语义。
- 本地 Java 源码看到 `BaseResponseEntity` 的 `{code,message,data,timestamp}` 形态和创建返回 Long ID；具体 Venus 版本/序列化、Bean Validation 错误、网关 HTTP 状态和所有 error body 仍需测试服务/UAT 验证。
- 当前 Java Campaign/Task create controller 源码未见 operation/idempotency key 接收、按请求 ID 查询或 unknown reconciliation 接口。Agent 侧头部配置不代表服务端具备幂等；确认该能力前所有真实写请求 gate 关闭，unknown 不可自动重发。
- 本地 operation 状态存在于 workflow draft，尚无子阶段 5 的 durable operation ledger；进程重启、checkpoint 原子性和跨进程重复保护没有完成。
- 尚未经测试服务或 UAT 完成 Campaign create、已有 Campaign detail/授权核验、Task create；尚未完成子阶段 TODO 要求的端到端外部测试服务/UAT 闭环，因此子阶段 2 不得标记 ✅。

### 后续执行入口（仍属子阶段 2）

1. 由 Java/API owner 签收 endpoint/base path、可信身份委托、品牌目录、error envelope、幂等键与按 request ID 查询/unknown reconciliation 契约。
2. 由业务 owner 确认品牌目录的 ID/code/name 权威字段及现有 Campaign 可访问性检查方式。
3. 凭据与测试服务/UAT 到位后，先只读验证品牌和 Campaign detail，再验证受控 Campaign create → detail 对账 → Task create；任何 unknown 先查询/人工对账，不重发。
4. 只有测试服务或真实 UAT 完成新 Campaign→Task 与已有 Campaign→Task 两条路径，且重复/超时不重复创建后，才可将本节标记 ✅。

完成标准：使用测试服务或真实 UAT 完成新 Campaign → 新 Task，以及已有 Campaign → 新 Task；超时/重复请求不会盲目重复创建。

## 子阶段 3：推荐启动、状态轮询、结果快照与恢复

目标：接入 Java recommend、task list/detail、results，区分 pending/in_progress/recommend_completed/failed，保存 downstreamTaskId 和结果快照引用。

依赖：子阶段 2、Java 轮询行为和结果完整性契约。

完成标准：推荐受理不显示完成；终态后才构建候选卡；重启/轮询失败/unknown 分支可对账。

### 当前进度：本地实现完成，真实联调和 durable 恢复未完成

本轮完成的本地实现：

- 增加 Java recommend、Task list/detail、results 的 typed request/response/error contract；recommend 按 Java 源码事实使用无请求体 POST，成功只映射为 `accepted`，不当作推荐完成。
- 增加独立的推荐 preview 和 `recommendation.confirm` 确认门。Task create 的确认不会被复用为 recommend 授权；确认绑定 Agent task、Campaign/Task、actor、operation/request、confirmation、form revision 和 canonical payload hash。
- 推荐写入前重新读取 Task，检查 Campaign/Task 归属、pending 状态、平台和下游 ID；adapter 单次发起 POST，不自动重试。超时、连接失败、无效受理和响应关联不明均进入 unknown/对账状态。
- 增加 `pending`、`in_progress`、`recommend_completed`、`failed`、`ended` 和未知状态映射；poll sequence 单调递增，旧/重复 poll 不覆盖新状态，`downstreamTaskId` 变化会阻断恢复。
- 增加有界的只读错误处理和恢复决策：连续四次可重试读取失败后进入 `reconciliation_required`；unknown 写结果不会因看到旧 pending Task 而再次启动 recommend；恢复决策只允许查询、读结果或人工对账边界。
- 仅在 Java `recommend_completed`、Task/结果关联校验通过后生成候选投影和快照引用。结果正文限制大小和候选数量；模型侧只保留最多 20 条安全字段预览，推荐理由截断，非 HTTPS URL 丢弃。
- 增加进程内 `VolatileRecommendationSnapshotStore`，用于本地测试和当前进程内引用验证；它不代表数据库、checkpointer、跨进程恢复或 Java 不可变结果快照。

本地验证：`.venv/bin/python -m pytest -q`（40 passed）、`.venv/bin/python -m compileall -q app tests`、`git diff --check`。覆盖 recommend bodyless POST、独立确认、受理与完成分离、unknown/no-retry、乱序/重复 poll、Task/results typed mapping、大小上限、候选投影、volatile snapshot 和 LangGraph 预览/启动/轮询边界。

### 尚未完成 / 阻塞

- 子阶段 2 的 Java 部署地址、Venus OAuth 委托身份、品牌授权传递、服务端 operation/idempotency/request ID 和 unknown 对账契约仍未签收；recommend 写 gate 默认关闭，本轮没有执行真实写请求。
- Java results 源码尚未证明候选全集、分页、总数、稳定 snapshot/version/hash 或不可变结果读取契约；当前投影明确标记 `candidate_universe_complete=false`，不能声称候选全集完整。
- `VolatileRecommendationSnapshotStore` 不是 durable snapshot；真实重启、断线、跨进程恢复、事件去重、worker poll、租约和 checkpoint 属于子阶段 5。
- 尚未通过测试服务或 UAT 完成 recommend start → Java polling → results 的真实链路；本地 40 passed 不是外部验收证据。

阶段状态：子阶段 3 只完成本地 contract、状态流转、受限结果投影和可序列化恢复决策；由于真实联调、结果完整性契约和 durable 恢复未完成，子阶段 3 整体暂不标记 ✅。后续仅继续了下方子阶段 4 的本地不可写实现。

## 子阶段 4：候选审阅与最终封板确认

目标：区分 AI recommend、backup、用户 selection 与最终确认；最终封板采用 `batch-confirm-selection` 语义，校验 snapshot/revision/selected IDs。

依赖：子阶段 3、候选全集和最终确认业务契约。

完成标准：只有用户明确确认才封板；普通 `batch-select` 不被误认为最终确认；重复/过时确认可拒绝或返回既有回执。

### 当前进度：本地实现完成，整体未验收

- `selection_review` 只分页浏览已读取的进程内 snapshot，每页最多 20 条。区分响应已取得、已读取条数、预览条数、截断和未知全集总数；本地 offset 不是 Java cursor，本地 UUID/hash 不是 Java 版本。实际结果投影固定 `candidate_universe_complete=false`。
- `selection_patch` 只编辑当前 workflow/Campaign/Task/run 的 Agent draft。保留字符串 KOL ID 和显式选择、取消、拒绝及拒绝理由；AI shortlist/backup 与 Java 已读圈选、本地草稿、最终名单分别处理。名单编辑递增 selection revision，轮换两张预览卡；旧 actor/任务/快照/版本/确认无效，空名单不可封板。
- `selection_save` 和 `confirm_selection` 分别绑定当前保存卡与封板卡，各有独立状态、preview、operation/request/confirmation ID 和请求类型。普通保存授权未签收；不完整候选或缺 Java snapshot/revision 校验时禁止封板。两条 adapter gate 都是硬关闭，graph 不分发 selection 写入；不完整结果仍可本地审阅和编辑。
- 覆盖本地事件重复、内容冲突、候选外/重复 ID、跨 workflow/Task、过期卡、迟到 poll、unknown/过期回执和恢复不自动写入。回执归约仅用 fixture 验证，仍无真实对账入口或持久化台账。补齐 Recommend gate 对 confirmation ID、form revision、payload hash 三个 header 配置的要求，缺任一配置零 HTTP 请求。
- 本地验证：`.venv/bin/python -m pytest -q`（66 passed）、`.venv/bin/python -m compileall -q app tests`、`git diff --check`。本轮未发送真实 Recommend、`batch-select` 或 `batch-confirm-selection` 写请求，子阶段 2、3 整体仍未验收。
- 外部阻塞：候选全集、分页和稳定快照/版本，Java 校验 selection revision/snapshot，普通保存授权，真实对象权限、重复回执、已封板行为、幂等和 request ID 对账均未签收。会话切换编排、durable ledger、Outbox、worker、lease/fencing、SSE 与跨进程恢复留待子阶段 5；本轮暂停，不进入子阶段 5。

### 新增设计准入项：表单回显与多 workflow 前端协议（尚未实现）

- 明确一个 conversation 可以包含多个 workflow；每条用户消息首版只路由到一个当前 workflow；新建活动/任务创建新 workflow，切换已有对象先校验 actor、tenant、brand、对象唯一性和权限。
- 子阶段 4 本地 selection review 以一个明确 business task/workflow 为边界；不把整个 conversation 限制为单 workflow，也不把多个 task ID 塞入一个 final confirmation。
- 在开始前端实现前，定稿 `form_snapshot`、`form_patch_applied`、workflow ID、event ID、seq、source、timestamp、form revision、field sources、validation errors 和 pending UI 的白名单事件协议。
- 表单回显优先采用服务端校验后的 typed snapshot/patch + Vue3 `ref`/`reactive`/Pinia；服务端 revision 是权威，旧事件和冲突 patch 不得覆盖用户编辑。Playwright 只作为 E2E 或不可修改外部页面的适配方案，不进入生产 Agent 执行链路。
- 子阶段 4 已完成不可对外写入的候选 review、selection draft、revision 校验和不完整结果禁封状态；真实 `batch-select`/`batch-confirm-selection` 继续受 Java contract gate 约束。

### 新增设计项：输入分流与意图提取（本轮只设计，实施时机待 LLM 接入）

- 现有 `interpret_event` 在未来最小调整为 `dispatch_input` 职责即可，不新增重复分流节点：结构化事件绕过模型，`UserMessage` 才进入 intent/proposal/validate 流程。
- `prepare_context` 后提供 actor、当前 workflow/Campaign/Task、form revision、pending operation/confirmation、stage 和有界上下文；模型只输出受限 typed proposal，不直接修改 AgentState、业务状态、确认或 graph 路由。
- proposal 至少覆盖 intent、受控 target、FormPatch/query、ambiguities、missing fields、evidence/source span、不确定性和建议澄清问题；校验失败、模型超时、schema 失败或模型不可用必须回到可恢复错误/人工结构化输入。
- 意图识别不阻塞当前子阶段 4 的结构化事件本地流程；在真实 LLM 自然语言能力接入时实施，并单独区分 fake model/schema 测试与真实模型效果评估。

### 新增设计项：语音转文字（本轮只设计，未纳入当前验收）

- 设计流程为浏览器录音 → 批量 ASR → 输入框草稿 → 用户编辑并点击发送 → 普通 `UserMessage` → 输入分流/意图提取。识别完成不自动发送，不直接触发任何业务副作用。
- 首选 `getUserMedia` + `MediaRecorder` 的一次性批量识别；云端 ASR、自托管 faster-whisper/Whisper 和独立 ASR 服务需通过中文/英文混说、金额/品牌术语、合规、延迟、成本和容量 PoC 后决定。
- 原始音频默认不进入 AgentState、消息历史或日志；语音权限、HTTPS、格式/大小/时长、取消、超时、失败和重复发送需有状态与错误协议。
- 语音只作为后续前端候选扩展，不新增当前完成勾选，不作为子阶段 2–4 或第一阶段当前验收前置。是否纳入第一阶段前端体验需另行确认。

## 子阶段 5：会话持久化、事件、checkpoint、Outbox、SSE 与鉴权

目标：补齐 PostgreSQL schema、事件去重、会话列表/恢复、LangGraph Postgres checkpointer、租约/fencing、Outbox、worker、SSE 和可信身份/品牌授权。

依赖：子阶段 1–4 的状态和 operation 结构稳定；PostgreSQL、认证服务和部署环境可用。

完成标准：刷新/重启/断线可恢复；同一事件不重复副作用；无跨品牌或未授权读取。

### 当前进度：部分实现，未完成验收（2026-11-20）

- 新增 PostgreSQL 迁移 `migrations/001`–`004`，包含会话、workflow、消息档案、入站事件、UI 事件、operation、selection revision、结果快照、Outbox、租约和摘要表；迁移按版本执行并以数据库 advisory lock 串行。首次在已有未登记 Agent schema 上执行时拒绝自动初始化，避免破坏性覆盖；真实历史 schema 升级尚未验证。
- 已有会话/消息读取、入站事件 canonical hash 与数据库唯一约束、表单事件的事务应用，以及独立测试用 Outbox worker。输入事件、消息和 Outbox 同事务提交；表单 draft/revision、事件回执、UI snapshot 和通知 Outbox 同事务提交。仅 `form_patch` 已接入该 worker；其他业务事件和业务写操作不支持自动执行。
- 新增 Postgres 结果快照存储、operation/selection ledger 写入基础、摘要引用和消息只增不改约束；这些尚未全部接入主执行链。正式目录/序列化策略、恢复对账和完整上下文构建未完成。
- `build_graph` 可注入官方 PostgreSQL saver，`wait_for_user` 使用 LangGraph interrupt。测试库验证了 saver 关闭再重建后的 Pydantic draft/event 往返和 resume；但现有 `WorkflowService`、checkpoint、worker fencing 和业务台账尚未组成完整的受控恢复链，不能宣称完成生产或跨进程业务恢复。
- 已有 UI 事件补拉、心跳、Last-Event-ID 解析，以及依赖可注入 authenticator/brand authorizer 的会话 API；默认无提供方时 API 返回 503。真实身份/品牌服务尚未联调；SSE 事件保留期、cursor 失效的权威 snapshot 恢复和完整 selection/form patch 协议仍未完成。
- 测试数据库上验证了首次迁移和重复迁移、并发重复事件、事务状态、租约与过期 fencing、快照隔离、模拟鉴权撤销、SSE 基础补拉和 checkpoint interrupt/resume。真实 Java 幂等、发送后崩溃窗口、跨进程恢复、生产认证和全部事件处理未验证；子阶段 5 **未完成**。不启动子阶段 6。
- 新增依赖 `psycopg[binary]>=3.2,<4` 和 `langgraph-checkpoint-postgres>=3.2,<4`；测试显式使用 `AGENT_TEST_DATABASE_URL` 指向独立 PostgreSQL 测试库，未配置时数据库测试跳过。未发送真实 Campaign/Task/Recommend/selection 写请求。

## 子阶段 6：长期记忆、前端集成与第一阶段整体联调

目标：实现结构化 user/user_brand 记忆管理、来源/撤销/防复活、Agent 左侧面板、历史会话、候选卡、SSE 重连，并完成真实端到端验证。

设计新增但尚未实现的前端项：Vue3 页面通过服务端白名单 `form_snapshot`/`form_patch_applied` 更新 `ref`/`reactive`/Pinia 状态；定稿 workflow ID、event ID/seq、source、timestamp、form revision、provenance、validation errors 和 pending UI 协议；测试用户编辑与 Agent patch 冲突、旧/重复事件、刷新/SSE 补拉及回显不提交。一个会话包含多个 workflow、通常一个当前交互 workflow；新建/切换旧 workflow 不得覆盖其他 workflow。具体前端工期需前后端评估，语音 ASR 仍只设计且不属于本子阶段默认完成条件。

依赖：子阶段 5、前端工作台仓库和业务/QA 验收数据。

完成标准：实施计划与技术文档中的第一阶段需求全部有真实代码、测试和联调证据；随后执行整体完整性检查并停止，不进入第二阶段。
