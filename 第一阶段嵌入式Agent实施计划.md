# SmartProposal 第一阶段嵌入式 Agent 实施计划

> 状态：评审稿。周期为初步估算，依赖正式 Campaign/推荐/名单确认服务契约。`Downloads/SmartProposal DEMO` 与 `Downloads/SmartProposal Agent DEMO` 都是原型材料，不是真实业务代码；本计划假设 Campaign 管理和创建能力已存在，本次实施新增 Agent 并编排现有能力。2026-11-18 已在 Smart Social UAT 实测 Campaign 创建、任务创建、推荐启动、推荐结果和 `batch-select` 圈选，未据此推断最终确认接口。

## 1. 交付目标

在 SmartProposal 现有工作台新增左侧嵌入式 Smart Proposal Agent，参考原型布局、收起/展开和会话交互。运营人员以自然语言完成 Campaign 创建和达人推荐流程；歧义必须交还用户澄清；Agent 通过既有服务创建 Campaign/推荐任务并取得推荐结果；最终卡片展示模型拟选名单及完整候选池，用户调整和确认后调用既有名单确认服务。交付还包含会话列表/恢复、持久化状态、权限与幂等。技术栈为 Python、LangGraph、PostgreSQL；pgvector 在线召回需另行确认。

## 2. 前置决策与依赖

启动前完成：

- 明确正式接口契约：Campaign create/detail/edit、task create/list、recommend trigger、task polling、results、batch-select 及最终名单确认；UAT 已观察到推荐启动成功后仍需轮询，结果页有推荐/备选分组。
- 明确登录主体如何映射 tenant/user/brand，提供服务端品牌授权检查。
- 盘点并取得既有 Campaign create/update、推荐任务 create/update、KOL recommendation/candidate listing、名单确认 API 契约、负责人和测试环境。
- 确认 Campaign 创建/保存既有交互和名单确认的副作用/确认规则；当前 Agent 决策要求 Campaign create、Task create 和 Recommend start 各自独立确认，具体与既有业务交互的衔接仍需 owner 签收；不另建平行业务真相表。
- 明确候选快照稳定性、结果全集与“查看更多”边界、候选总数和分页、KOL 稳定 ID、`source/shortlist/backup` 权威语义、模型拟选集合、可展示字段及名单确认约束。
- 明确活动与任务的预算/KPI/Tier 字段语义、品牌默认和强制规则来源；定义歧义澄清策略。
- 明确历史会话列表/跨品牌访问/删除与保留策略。
- 明确一个 conversation 包含多个 workflow、当前交互 workflow 指针、单条用户消息只路由到一个 workflow，以及新建/切换/恢复的目标解析规则。
- 明确自然语言输入分流/typed proposal、Vue 表单 snapshot/patch 回显、form revision 冲突及 SSE/API 事件协议。
- 语音输入目前只做设计；是否纳入第一阶段实现，需经过 ASR 质量/隐私/成本 PoC 和单独产品确认。
- 明确首阶段是否有批准的历史案例语料和向量检索用例；若无，pgvector 不进入请求关键路径。
- 用户记忆采用 PostgreSQL 结构化偏好为权威数据源；允许并行评估自托管 memU 核心库作为语义召回投影，生产引入以隐私隔离、撤销和效果测试通过为前提。Hy-Memory 2.0 为后备；其他方案比较见技术文档。
- 提供 UI 面板布局评审、试点运营人员及 100–200 条验收表达的业务评审人。

未拿到真实 KOL 契约时，可先构建 mock adapter 和字段契约测试，但不能据此验收线上联调完成。

## 3. 分阶段计划

| 阶段 | 主要工作 | 阶段验收/退出条件 |
|---|---|---|
| 0. 正式业务契约与 UX 定稿（约 4–6 个工作日） | 盘点正式 Campaign、推荐任务、推荐候选和名单确认服务；确认字段/状态/幂等/权限及既有确认语义；设计歧义澄清与双区块候选确认卡；定义会话恢复和数据策略 | API/字段及各阶段副作用签收；候选快照完整性可验证；确认业务 owner、试点负责人和验收样例 |
| 1. Agent 持久化与事件骨架（约 4–6 个工作日） | Python 服务、conversation/message/event/outbox 和 Campaign operation/recommendation run/selection confirmation 台账；身份 guard、幂等、checkpoint 生命周期、列表/快照/SSE 补拉 | 不接模型可受理/去重事件、列出/恢复会话；checkpoint 恢复；测试确认租约和事务提交点；迁移可重复部署 |
| 2. 领域模型与既有服务 Adapter（约 5–7 个工作日） | Campaign/产品/任务 schema、patch/provenance、歧义规则；现有 Campaign/任务/推荐/候选/名单确认 Adapter；分阶段 idempotency/unknown/reconciliation | 人工输入通过测试服务完成 create campaign → task → recommend → candidate query → confirm selection；重复和超时不产生重复副作用 |
| 3. LangGraph 全流程编排（约 5–7 个工作日） | 自然语言输入分流、typed proposal/validate、跨字段澄清、按既有 Campaign 语义创建、分阶段调用、interrupt/resume、候选名单读取/必要时模型选择、持久化恢复、节点专用上下文 | 结构化事件绕过模型；模型提议不能绕过 patch/revision/confirmation；歧义不会触发下游；最终名单未确认不可提交。意图识别的真实模型效果另行评测 |
| 4. Agent 前端与会话体验（约 6–9 个工作日） | 左侧面板、多个 workflow 列表/当前 workflow 切换、历史列表/新建/恢复、Campaign 字段审阅、结构化 form snapshot/patch 回显、来源/错误显示、阶段进度、候选分页和 selection draft、SSE 重连 | 本地可先验收 Vue 回显、revision 冲突、候选 review 和不可写 selection draft；完整真实名单确认、`batch-select` 和封板需等 contract gate/UAT，不把 mock 写入当真实验收 |
| 5. 长期记忆与轻量组件评估（约 3–5 个工作日，按需裁剪） | user/user_brand 结构化精确偏好；明确设置/撤销/证据/版本；可并行开展 memU 自托管小样本 PoC | 记忆隔离和撤销测试通过；向量 PoC 达标后另定范围；无获批语料不启用在线召回 |
| 6. 联调、评测与试点（约 5–8 个工作日） | 各正式 API 联调；权限/重复/并发/断点恢复/候选完整性测试；100–200 条运营表达评测；灰度和告警；若语音获批再加 ASR 验收 | 无重复业务对象/名单提交；无跨品牌泄漏；表单冲突、确认阻断和恢复测试通过；业务签收和回滚方案就绪 |

粗略总计约 6–8 周（一个后端、一个前端、业务兼职验收并行），这是原有范围的初步估算；不代表已估算本次增加的多 workflow 前端状态、form snapshot/patch 协议和冲突处理工作量。阶段可并行的前提是正式业务/API 契约先冻结；若现有服务不提供幂等、候选快照或全量分页，需增加适配改造时间。新增前端协议和 Vue 接入需由前后端按定稿方案重新估算；语音 ASR 不计入当前估算，只有 PoC/合规通过且产品明确纳入后另行估算。长期记忆行为候选自动化、历史案例向量搜索可拆到试点后。

新增设计的实施边界：输入分流与意图提取先完成设计，在真实 LLM 自然语言能力接入时实现，不阻塞当前内部子阶段 4 的结构化事件流程；Vue 表单回显协议需在前端实现开始前定稿，真实页面集成归入原计划阶段 4，持久化 envelope 与 SSE 补拉归入原计划阶段 1；语音只做设计，ASR PoC/合规确认后再决定是否增加独立实现里程碑，当前不计入第一阶段验收或原工期。内部子阶段 4 的候选 review/selection draft 仍按 TODO 记录执行，不改写本表总体阶段编号。

## 4. 建议任务拆分

### 后端

- FastAPI/Pydantic API、可信身份上下文和 conversation/brand 授权。
- PostgreSQL 迁移、会话/事件/operation/recommendation run/candidate reference/selection confirmation/Outbox repository。
- Campaign 与推荐任务草稿 schema、patch/provenance/default resolver、revision/hash 和歧义分类。
- LangGraph state/nodes/routing/interrupt/checkpointer 与上下文预算。
- Campaign/任务创建、推荐、候选分页、名单确认 Adapter；各阶段稳定幂等、异步轮询和 unknown 对账。
- 候选快照完整性校验、模型拟选 ID 子集校验、选择集版本及最终确认台账。
- 会话事件串行化、数据库租约/fencing token、Outbox worker。
- 结构化审计、脱敏、指标、故障分类和数据留存执行。

### 前端

- 按组合原型在 SmartProposal 主工作台集成左侧 Agent 面板及收起/展开，不替换产品导航；自然语言联动既有 Campaign 创建流程和正式 API。
- 会话创建、历史列表/分页、恢复、品牌上下文展示。
- 聊天、歧义澄清、Campaign 字段审阅、字段来源、revision 预览和阶段性确认。
- 推荐进度及最终卡片：模型拟选名单、全部候选的分页/搜索、用户编辑选择、总数和确认回执。
- SSE 事件序号、重连补拉、请求/任务状态、错误和空/加载状态。
- 窄屏面板折叠及面板关闭后的后台任务持续状态体验。
- 表单 snapshot/patch、workflow ID、event ID、seq、source、timestamp、form revision、字段来源和校验错误的白名单事件协议；前端用 Vue reactive/Pinia 应用，不通过 DOM 自动化驱动生产表单。
- 语音若经后续确认纳入，前端只负责权限、录音状态、取消和输入框草稿；ASR 结果必须由用户检查并点击发送后才进入普通 UserMessage。

### 业务/产品/QA

- 提供 Campaign/推荐任务字段、枚举、单位和品牌规则；定义 Campaign 创建和最终名单确认副作用/取消语义。
- 确认候选字段可见性、全量分页、模型解释证据、用户调整与名单提交规则。
- 准备脱敏运营用例和每例预期字段 patch、歧义处理、记忆结果。
- 验收结果是否忠实于接口返回、预算/筛选解释是否准确。
- 评审试点用户、灰度策略、退出条件和反馈处理人。

## 5. 测试与发布门槛

### 必需测试

- 表单 patch：只改目标字段、列表增删/清空、未设置与不限的区别、来源更新和 revision 递增。
- Campaign 和推荐流程：产品/活动字段映射、多轮补齐、Budget/KPI 歧义澄清、Campaign create 幂等、已创建后失败续跑不重复创建。
- 推荐名单确认：候选快照完整性/总数/分页、模型拟选只引用候选 ID、用户增删/替换/排序递增 revision、过时快照/确认拒绝、重复确认不重复提交。
- UAT 回归：可选周期和活动总预算留空可创建；任务创建、推荐受理、轮询完成和 results 读取分别验证；初始 2 位与展开后 4 位不误标为全集，推荐/备选标记与用户圈选分别展示。
- 圈选和最终确认：在业务确认 `batch-select` 副作用前仅编辑 Agent 草稿；确认契约后测试选择、取消、重复请求及刷新恢复，最终名单只能在用户明确确认后提交。缺少完整候选或确认契约时阻断端到端验收。
- 推荐理由按不可信文本处理，验证产品信息向下游 brief 的映射；指标缺失、多个报价及不支持的条件均须如实展示，不以链路成功代替推荐质量验收。
- 业务规则：预算歧义不猜测、枚举拒绝未知值、硬规则不可被偏好覆盖、接口不支持条件不被伪称已应用。
- 输入分流与意图提取：结构化事件绕过模型；typed proposal 的字段白名单、revision、target workflow、歧义和失败降级；模型不能产生确认或直接副作用。
- 表单回显：连续 snapshot/patch、手动编辑与 Agent 冲突、旧/重复事件、SSE 重连、恶意字段和“回显不提交”。
- 语音设计 PoC（仅在批准后）：权限、中文/英文混说、金额/日期/品牌术语、噪声/静音、超时/取消、用户未发送不产生 UserMessage、音频不入日志和 AgentState。
- 多轮图：新会话、interrupt/resume、已 END 后新输入、重复事件、同 thread 并发输入、进程重启恢复。
- 多 workflow 会话：当前 workflow 默认路由、明确新建后切换、明确继续旧 workflow、同名/未知目标澄清、旧 workflow 后台状态不被覆盖或自动重启。
- 历史会话：列表权限、详情权限、跨 brand 隔离、恢复旧 pending UI、消息分页/游标和 schema 兼容。
- 分阶段操作：Campaign create payload/schema 冲突、歧义阻断及重复事件；推荐超时 unknown、候选快照变化、最终名单双击确认、下游已接受后服务崩溃、异步轮询和中途恢复。
- 记忆：临时覆盖、user_brand 隔离、明确更新/撤销、乱序旧事件、防删除后复活。
- 若启用向量：元数据过滤先于排序、跨 tenant/brand 隔离、删除及重建一致、低相关结果不自动写表。
- memU PoC：对照精确查询测量召回质量、错误写入、P95 和成本；验证品牌/用户隔离、撤销后索引删除及重建、延迟任务不复活、组件故障回退；生产使用前锁定版本和 embedding 配置。

### 发布阻断项

- 任何未授权会话、Campaign、任务、候选、结果或记忆读取及跨品牌数据泄漏。
- Campaign/task 被重复创建、未确认名单被提交、过时名单确认可执行、unknown 状态被盲目重发。
- interrupt 后无法恢复，或从历史会话恢复后丢失用户待处理问题/当前表单。
- 模型可以绕过用户确认、品牌强制规则或字段白名单。
- 业务验证集中的严重错误未有明确拒绝/澄清处理；不可用平均分掩盖金额、品牌等高风险字段错误。

语言理解指标（如显式字段抽取准确率、明确记忆写入精确率）、P95 延迟和单任务成本需在评测集和真实模型/接口确定后由业务设目标。涉及明确长期偏好的写入应优先高精确率，拿不准时不写并澄清。

## 6. 上线与回滚建议

- 先在测试/预发布接真实 Campaign/推荐/名单确认服务，验证幂等和超时处理；mock 结果不能代替外部联调。
- 灰度仅开放给明确授权的试点用户和品牌；Agent 仅通过已审查的 Campaign/task/recommendation/selection-confirmation 服务操作业务对象，不提供任意写能力。
- 模型/prompt/schema/graph 版本进入日志；图状态 schema 变化要验证旧 thread 恢复，必要时保留旧图版本或显式迁移。
- 功能开关可按用户/品牌关闭 Agent 面板入口；关闭前已受理的外部任务仍通过 request 状态可查询。
- 回滚应用版本前保持向后兼容的数据库迁移；不得靠删除 checkpoint 或清空会话数据恢复。
- 若 pgvector 案例检索造成低质量召回，可独立关闭检索而不影响结构化记忆、历史会话和 KOL 推荐闭环。

## 7. 评审签收清单

- [ ] 产品确认嵌入入口、历史列表/恢复、品牌切换及会话删除体验。
- [ ] 业务确认 Campaign/选人任务字段、歧义规则、既有 Campaign 保存/确认步骤（Agent 不增加确认门槛）、`batch-select` 是暂存还是正式提交、名单最终确认点及不支持字段反馈。
- [ ] 后端确认既有 Campaign/任务/推荐/候选/名单确认 API 的身份权限、幂等、查询恢复与数据保留策略。
- [ ] 前后端确认 Agent 如何调用既有页面业务服务，以及推荐/备选分组、完整结果读取、用户暂存选择和最终确认卡片协议。
- [ ] 数据负责人确认首阶段 pgvector 在线用途；若无用例，签收“仅能力预留，不做在线召回”。
- [ ] 业务、数据和研发确定 memU PoC 数据、评测阈值与结果归属；未通过隔离或撤销测试则不接入生产。组件选型及其额外工期以 PoC 结果为准。
- [ ] QA/业务提供并签收评测语料、期望行为和发布阻断项。
- [ ] 运维确认模型凭证、PostgreSQL/pgvector 扩展、连接池、worker、日志脱敏、告警和备份恢复方案。
