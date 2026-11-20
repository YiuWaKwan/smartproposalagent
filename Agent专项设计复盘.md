# Agent 专项设计复盘

> 复盘日期：2026-11-19
>
> 当前开发位置：内部实施子阶段 1 已完成；子阶段 2 的本地 typed adapter contract、本地双确认流程与 contract tests 已实现；子阶段 3 的本地 recommend/轮询/结果投影和恢复决策已实现，真实 Java/UAT 契约、写联调和 durable 恢复仍未完成。以下第 9 节产品和架构决策保持原意；新增实施记录只更新代码状态，不改变决策。
>
> 结论中的“代码事实”只代表当前仓库实际存在的实现；“设计已考虑”代表设计文档已有明确约束，但当前代码可能尚未实现；“建议调整”代表需要在后续实现前补充边界或修改设计表达。

## 1. 复盘范围

本次重新阅读了：

- [第一阶段嵌入式Agent实施计划.md](第一阶段嵌入式Agent实施计划.md)
- [第一阶段嵌入式Agent技术实现文档.md](第一阶段嵌入式Agent技术实现文档.md)
- [BUSINESS_CODE_INDEX.md](BUSINESS_CODE_INDEX.md)
- [CODE_INDEX.md](CODE_INDEX.md)
- [第一阶段内部实施子阶段TODO.md](第一阶段内部实施子阶段TODO.md)
- [app/graphs/campaign/state.py](app/graphs/campaign/state.py)
- [app/graphs/campaign/graph.py](app/graphs/campaign/graph.py)
- [app/graphs/campaign/nodes.py](app/graphs/campaign/nodes.py)
- [app/services/draft_service.py](app/services/draft_service.py)
- [app/services/contracts.py](app/services/contracts.py)
- [app/services/workflow.py](app/services/workflow.py)
- [app/schemas/domain/workflow.py](app/schemas/domain/workflow.py)
- [app/schemas/events.py](app/schemas/events.py)
- [app/schemas/patch.py](app/schemas/patch.py)
- [app/main.py](app/main.py)、[app/config.py](app/config.py)、[pyproject.toml](pyproject.toml)
- [tests/test_draft_workflow.py](tests/test_draft_workflow.py) 和 [tests/test_base.py](tests/test_base.py)

业务边界对照了 `BUSINESS_CODE_INDEX.md` 索引的 Java Campaign/Task Controller、Campaign/Task/AiRecommend 服务、Retrofit、推荐轮询、结果合并和圈选确认代码，以及 Python 下游 presets/tasks/run graph/security 代码。

本次重点检查：

- AgentState、业务草稿和事件的职责边界。
- LangGraph 节点、路由、恢复位置和后续副作用边界。
- 消息历史、模型调用、工具调用和提示词接入位置。
- Java adapter 即将使用的字段映射、错误、幂等和安全上下文。
- Campaign、Task、推荐启动、候选结果、普通圈选和最终封板的副作用语义。
- 上下文压缩、KV Cache、ACI、参数保真、提示注入和危险操作控制。

本次没有覆盖：

- 真实模型供应商的上下文窗口、缓存计费或 KV Cache 行为。
- PostgreSQL、LangGraph Postgres checkpointer、SSE、Outbox 和租约的运行验证。
- 真实 Java/UAT adapter 联调、认证链路和下游超时行为。
- 前端工作台代码和实际消息渲染实现。

## 2. 总体判断

当前方案可以继续进入子阶段 2 的准备工作，但不建议在以下事项未明确前直接对真实环境发起有副作用的 Java create/recommend 请求：

1. Java API 的认证上下文如何从 Agent 传递，尤其是 tenant、user、brand 和服务间凭证的边界。
2. Campaign create、Task create、recommend 启动缺少下游幂等能力时，Agent operation 台账和 unknown 对账如何落地。
3. `batch-select` 与 `batch-confirm-selection` 的产品确认入口、重复提交回执和最终封板语义。
4. Campaign `brand_id/brand_name` 与 Java `brandName`/后端 `brandCode` 的可信映射。
5. Java 返回的 error envelope、HTTP 状态、业务 code 和超时分类。

这些问题不是当前子阶段 1 的实现错误。子阶段 1 只完成了本地确定性 patch、revision 和校验；[第一阶段内部实施子阶段TODO.md](第一阶段内部实施子阶段TODO.md) 已把服务契约和幂等作为子阶段 2 的依赖。本次复盘建议在子阶段 2 开始时先完成一个“只读/无副作用 contract adapter + operation 设计”小步骤，再实现真实创建调用。

当前没有发现需要改变 LangGraph、Python 或 Pydantic 作为核心技术选型的理由。需要补强的是消息/上下文模型、工具和 adapter 的安全契约，以及持久化之前的状态一致性设计。

## 3. 逐项审查

### 3.1 上下文管理

**适用性：适用，且是第一阶段必须处理的运行时能力。**

**设计依据和代码证据：**

- 技术文档 §6.3 已规定节点专用 Context Builder、固定规则/schema、当前表单/provenance、pending UI、任务摘要、近期完整轮次和输入预算。
- 技术文档 §6.3 已规定超限时先移除可选案例、旧结果说明和较早对话，保留最新输入、当前表单、关键规则和 pending UI。
- 技术文档 §6.3 已规定结构化摘要保留 `task_id / decisions / rejected_options / unresolved / source_message_ids / covered_until_message_id`，并要求金额等关键数字与结构化表单交叉校验。
- 技术文档 §7、§4.3 已区分业务消息档案、checkpoint、业务台账和模型上下文。
- 当前 [AgentState](app/graphs/campaign/state.py) 只有 `draft`、`incoming_event`、版本、少量引用和结果字段，没有 `messages`、任务摘要、摘要水位、工具调用记录或 memory refs。
- 当前 [WorkflowDraft](app/schemas/domain/workflow.py) 保存的是业务草稿、来源、校验问题、pending UI 和选择摘要，不保存聊天历史。
- 当前代码没有 Context Builder、模型客户端、摘要器、消息仓库或持久化 checkpoint。

**已经覆盖：**

- 设计层面已经明确“业务状态不等于模型上下文”。
- 子阶段 1 的 `WorkflowDraft` 已将 Campaign/Task 事实、revision、来源和校验结果结构化，后续可以作为上下文压缩后的权威事实。
- `pending_ui` 和 `form_revision` 已有类型位置，可用于恢复澄清问题和避免旧表单覆盖。

**尚未覆盖和风险：**

- 当前没有消息历史，因此还没有验证长对话超过模型窗口时如何裁剪。
- 当前没有标准消息列表，也没有保存 assistant、tool call、tool result 的配对关系；后续接入 LLM 时不能把 `incoming_event` 直接当作完整消息历史。
- 当前没有摘要覆盖水位。若只保留摘要而不记录 `source_message_ids` 和 `covered_until_message_id`，可能丢失“用户否定了什么、确认了什么、哪些任务仍未完成”。
- 当前没有超长 API/工具返回的引用机制。Java results 可能包含完整 KOL 明细、理由文本、指标和报价；不能将完整 JSON 无界地追加到模型消息。
- 当前没有 checkpoint 序列化测试。`AgentState` 中包含 Pydantic 对象和事件联合类型，未来使用 Postgres saver 时需要验证往返后的类型恢复，不要假设一定保持为 Pydantic 实例。
- 当前 graph 的 `wait_for_user` 仍直接抛 `WorkflowNotImplemented`；`wait_for_user → prepare_context` 的静态边还没有真正 interrupt/resume 语义。

**结论：后续实现，且需要在接入 LLM 前完成上下文边界设计；不是当前子阶段 1 的缺陷。**

**建议：**

- 以业务消息表作为完整历史事实源，以 checkpoint 保存可恢复的工作状态，以 Context Builder 生成每次模型调用的有限上下文。
- `AgentState` 增加稳定的 `messages` 工作窗口、`task_summary`、`summary_covered_until_message_id`、`pending_ui` 和必要的 tool/result 引用；完整消息不放进每次模型上下文。
- 工具/API 原始响应存为受控结果引用或业务结果快照，进入模型的只包含带长度上限的结构化摘要、字段白名单和引用 ID。
- 压缩后必须重新校验：当前 `form_revision`、Campaign/Task ID、operation/request 状态、候选 snapshot hash、用户确认状态、未解决问题和关键金额/日期。
- 中断恢复时先从业务表和事件台账重建权威状态，再恢复 checkpoint 工作窗口；不能只恢复 checkpoint 中的旧表单或旧记忆快照。

**建议处理时机：**

- 子阶段 2：确定 adapter 返回的结构化结果、摘要和原始结果引用，不把大响应直接交给模型。
- 子阶段 3：为 progress/result 做有界投影和结果引用。
- 子阶段 5：实现消息表、摘要、checkpoint、interrupt/resume 和恢复一致性。

**建议验证：**

- 构造超长多轮对话，验证压缩后金额、否定条件、当前 Task ID、revision、pending 问题和确认状态保持不变。
- 构造超长 Java results、错误响应和推荐理由，验证模型上下文只收到有界投影，原始结果仍可通过引用读取。
- 在 interrupt 前、interrupt 后重启、压缩后恢复、外部 operation 进行中恢复四种场景分别验证。
- 不只检查消息数量减少；应对比压缩前后的结构化事实、摘要覆盖范围和可执行路由结果。

### 3.2 KV Cache 前缀缓存与消息组织

**适用性：适用但属于优化项，不应阻塞子阶段 2。**

**代码事实：**

- 当前没有模型客户端、prompt 构建器、消息列表或工具定义，因此当前没有 KV Cache 已生效的证据。
- `pyproject.toml` 没有模型供应商 SDK 或统一模型网关配置。
- 当前 `AgentState` 的 `incoming_event` 是自定义 Pydantic 事件，不是 LangChain 标准消息列表；这适合确定性事件受理，但后续模型调用需要单独转换为标准消息格式。
- 当前图没有 append-only message reducer，也没有消息重排、重建或历史持久化逻辑可供验证。

**设计已经考虑的内容：**

- 技术文档 §6.3 要求固定指令和字段 schema 保持稳定顺序，动态数据按节点裁剪。
- 技术文档 §9 要求记录 model/prompt/schema 版本。
- 参考 Harness 的 system prompt 固定 section、动态 context、tool schema 分离，已写入技术文档 §13 的参考映射。

**尚未覆盖和风险：**

- system/developer 指令、动态业务 context、历史消息、工具定义和工具结果的分层尚未落地。
- 如果每次恢复都重新拼接全部历史、重新生成随机提示词或把时间戳/随机 operation ID 放到稳定前缀，可能降低缓存复用；但目前没有供应商证据证明这样会实际影响本项目成本或延迟。
- 尚未确定模型供应商是否支持 prefix/KV cache、缓存命中计费、跨请求复用条件、缓存失效规则或消息前缀要求。
- 工具定义的顺序、schema 序列化和描述版本尚未存在，无法验证稳定性。

**结论：保留扩展空间，当前不把 KV Cache 写成已生效能力。**

**建议：**

- 先保证正确的标准消息语义和可恢复性，再采用稳定 prompt section、稳定工具排序和规范化 schema 序列化。
- 固定前缀只放不依赖本轮的规则、schema 和安全约束；当前品牌、草稿、revision、pending UI、记忆和工具结果放在动态 context 区。
- 消息采用只增不改的业务档案；压缩通过新摘要/受控工作窗口完成，不重写历史事实源。
- 不为了缓存把用户确认、当前 revision 或业务权限放入过期的静态前缀。

**建议处理时机和验证：**

- 子阶段 3 之前确定 Context Builder 的 section 顺序和标准消息转换。
- 模型接入后由模型/平台负责人确认 cache 能力和计费规则，再做 A/B 或 trace 对比；验证前只记录为假设。
- 测试消息追加、重启恢复、工具 schema 变化和 prompt 版本变化时的行为，不以 token 数下降单独判定正确。

### 3.3 工具侧 ACI 设计

**适用性：适用，但当前阶段不应直接暴露通用工具调用。**

当前项目没有 Shell、文件系统、浏览器或任意数据库工具。第一阶段真正需要的是受控的 Java SmartProposal 业务能力。技术文档也明确不引入任意命令/文件工具、通用插件系统和多 Agent 生命周期。

**当前代码和设计证据：**

- [services/contracts.py](app/services/contracts.py) 只有 `BusinessServices.create_campaign/create_task` 两个 Protocol 方法，没有实现、参数 schema、错误类型、调用结果或 operation ID。
- [nodes.py](app/graphs/campaign/nodes.py) 的 `reserve_business_operation` 仍在 Java adapter 和 operation ledger 边界抛出未实现异常。
- `BUSINESS_CODE_INDEX.md` 已区分 Campaign create、Task create、recommend、progress/results、batch-select 和 batch-confirm-selection。
- 技术文档 §5.1、§6.2 已要求白名单事件、revision、snapshot、confirmation ID 和最终确认绑定。

**已经覆盖：**

- 业务能力边界已从通用工具中分离出来。
- 设计已要求确定性代码控制权限、枚举、金额、确认状态和路由。
- 设计已要求模型不能自行改变 tenant/user/brand、权限、业务对象 ID 或路由。

**尚未覆盖和风险：**

- 尚未定义工具/adapter 的结构化成功结果、业务失败结果、unknown 结果和可给模型看的摘要。
- Campaign create、Task create、recommend 启动和最终封板都可能产生副作用，但当前没有工具级风险等级或调用前检查协议。
- 如果直接把 Java API 封装成大而全的 `smartproposal_api` 工具，模型难以区分“创建任务”“启动推荐”“查询状态”“普通圈选”和“最终封板”。
- 如果把 Java URL、Bearer token、任意 path、任意 JSON 作为工具参数，模型就可能越过业务字段白名单。
- 完整 Java results、推荐理由和错误文本不适合原样进入消息历史。

**结论：建议调整子阶段 2 的 adapter/ACI 设计，但不需要改变第一阶段总体范围。**

**建议的 ACI 约束：**

- 不给 LLM 一个通用 HTTP 工具；由确定性 LangGraph 节点调用 typed adapter。
- 若后续确实需要模型选择工具，工具应是小而明确的只读或受控业务动作，例如：
  - `read_campaign`
  - `create_campaign`
  - `create_recommendation_task`
  - `start_recommendation`
  - `get_task_status`
  - `get_recommendation_results`
  - `confirm_selection`
- 每个工具的 schema 只允许业务字段和服务端生成的 operation/revision 引用，不允许 URL、token、tenant/user/brand 覆盖值或任意状态字段。
- 每个结果分为机器可处理结构和模型可读摘要。机器结果至少包含 `status/code/request_id/business_id/external_id/retryable/unknown`；摘要只包含下一步所需的有限信息。
- 工具描述必须明确副作用：只读、创建、异步启动、持久化圈选、最终封板。`batch-select` 和 `batch-confirm-selection` 必须是两个不同语义。
- 工具错误要分类为参数错误、权限错误、业务冲突、下游失败、超时 unknown 和不可重试错误；模型不应通过错误文本自行猜测重试策略。

**建议处理时机：**子阶段 2 先完成 adapter contract 和只读/纯映射测试；子阶段 3–4 才接副作用工具和确认边界。工具输出长度和原始响应存储在子阶段 3/5 处理。

### 3.4 参数保真

**适用性：适用，且是子阶段 2 的核心质量要求。**

**已经覆盖：**

- `CampaignDraft`、`RecommendationTaskDraft`、`FormPatch` 和 `FieldPatch` 是 Pydantic 类型。
- `apply_form_patch()` 会检查 `expected_form_revision`，并使用目标模型字段的 `TypeAdapter` 校验值。
- 金额使用 `Decimal`，日期使用 `date`，业务 ID 与下游字符串 ID 分开建模。
- `clear`、`use_default` 和 `suppressed_defaults` 已有不同代码路径。
- Java/Python 字段和类型转换已经在 `BUSINESS_CODE_INDEX.md` 记录：Java Campaign `brandName`、Task camelCase、Retrofit 下游 snake_case、预算 `BigDecimal → int → Python float`。

**当前缺口和风险：**

- `FormPatchEvent.task_id` 当前没有在 `apply_form_patch()` 中与 `draft.task_id` 比较；如果事件被送入错误 workflow，当前纯函数可能修改错误草稿。这是 adapter 前应补的事件归属校验建议。
- `set` 的 `value=None` 会被当作设置空值，而 `clear` 才会写入 `suppressed_defaults`；该语义需要固定为明确契约，避免模型把 `set null` 当作 clear。
- `use_default` 当前只解除 suppressed 标记，不实际解析和写入默认值，也不递增 revision；这是子阶段 1 有意未实现默认值服务，但在接入 LLM 前必须确定它是“请求重新应用默认值”还是“仅取消屏蔽”。
- `add/remove` 当前对所有字段都拒绝，因为现有字段都是 scalar；设计文档允许适用列表字段的操作，后续如果增加列表字段必须为每个字段定义元素类型和去重语义。
- 当前只做基础字段校验，没有平台枚举、品牌授权、Java DTO Bean Validation、币种/单位、任务配额和下游边界校验。
- `ActorContext` 只有 `brand_id`，而 Java Campaign create 需要可信 `brandName`，并由 Java 转换为 `brandCode`。这不是模型参数能自行解决的字段，需通过品牌目录/授权服务解析。
- 当前 adapter Protocol 返回裸 `int | str`，无法表达 Java envelope、业务 code、HTTP status、request ID、unknown 或原始错误引用。
- 当前 patch 的 `field_sources` 只写 `source=form_edit`。自然语言输入、用户直接表单编辑、记忆默认和品牌默认需要区分 source 类型及证据 ID。

**结论：当前基础类型已覆盖局部参数保真；建议在子阶段 2 前调整 adapter contract，并把事件归属、null/clear、brand 映射和结果 envelope 作为硬约束。**

**建议验证：**

- 每个 Java Campaign/Task 字段做双向 contract fixture，检查 JSON 名称、空值、日期、Decimal 金额、ID 类型和嵌套字段。
- 测试 `None`、`clear`、`use_default`、空字符串、空列表和未提供字段的区别。
- 测试错 task_id、错 campaign_id、错 brand、旧 revision、未知枚举、负金额、日期逆序和超出数量边界。
- 对 Java `code != 200`、HTTP 非 2xx、HTTP 200 但 data 缺失、连接超时和响应解析失败分别产生不同 typed error。
- 验证重试前后 canonical payload hash 不变，adapter 不擅自填充未提供字段或修改用户已确认值。

### 3.5 危险命令与危险操作语义

**通用 Shell/命令执行：当前不适用。**

当前仓库没有 Shell、文件系统、浏览器、代码执行或通用命令工具；第一阶段也明确排除任意命令/文件工具。因此本次不增加命令解析器、黑名单或 sandbox 设计。

**业务危险操作：适用。**

根据业务代码索引，风险等级应至少区分：

| 操作 | 当前业务性质 | 需要的保护 |
|---|---|---|
| 读取 Campaign/Task/结果 | 读取业务数据 | 服务端 actor/brand/campaign/task 授权；结果字段白名单；不接受客户端身份覆盖 |
| Campaign create | 创建正式业务对象；设计规定字段完整且无歧义时不增加 Agent 专属确认 gate | operation ID、canonical payload hash、权限和 Java envelope 校验；网络重试不能重复创建 |
| Task create | 在 Campaign 下创建新的选人任务；一个 Campaign 可有多个 Task | 绑定已授权 campaign、operation ID、明确 task draft、重复/unknown 对账 |
| Start recommendation | 触发异步推荐，Java 会创建/执行下游任务并占用并发名额 | 只能在 Task 已创建且条件完整时调用；request ID、downstreamTaskId、状态轮询和 unknown 对账；不能把成功响应当完成 |
| `batch-select` | 持久化逐 KOL select/cancel/reject 并写历史，不封板 | 必须区分用户编辑持久化状态和最终确认；绑定 task、KOL ID、选择 revision |
| `batch-confirm-selection` | Java 事务内圈选并封板任务 | 必须绑定明确用户确认、候选 snapshot、selection revision、confirmation ID；重复和过时确认拒绝/返回原回执 |
| Task update/recommend rerun | 可能清理旧结果并重新进入 pending/in_progress | 必须有明确用户修改和新 revision；旧结果/旧确认失效，不能由模型隐式触发 |

**结论：业务副作用安全已经在设计文档中覆盖，当前代码尚未实现。** 不需要 Shell 安全方案，但子阶段 2 的 adapter 不能把所有 Java POST 当成同一种低风险工具。

### 3.6 提示注入与不可信数据边界

**适用性：适用，尤其是接入 LLM 和 Java results 后。**

**设计已覆盖：**

- 技术文档 §6.3 明确引用数据中的指令不能改变 system policy 或确定性校验。
- 技术文档 §9 明确用户消息、历史记忆、模型输出和外部接口文本均按不可信数据处理。
- 技术文档 §2.3 明确推荐理由是普通字符串，不能 `eval`，不能作为结构化业务事实。
- 技术文档 §4.4、§5.1 明确模型不能改变身份、权限、业务对象 ID、路由和执行状态。
- 当前 `DomainModel(extra="forbid")`、字段 patch 白名单和确定性 `draft_service` 已限制结构化事件字段，但这不是完整的 prompt injection 防护。

**尚未覆盖和风险：**

- 当前没有 LLM、工具或外部文本进入 AgentState 的实现，因此没有运行证据证明提示注入已被阻断。
- Java API 返回的 Campaign/Task 名称、推荐理由、KOL 描述和错误文本可能包含指令性自然语言；未来必须作为 data block/typed result 进入 context，不能作为 system/developer message 或用户确认。
- `UserMessage.text` 当前只是保存为字符串类型，没有模型处理层、长度限制、敏感信息策略或注入检测边界。
- 工具结果如果写入普通 assistant/user message，模型可能把它误认为新的用户指令；必须使用标准 tool result 角色或结构化 context，并保留 source/type/operation 引用。
- “用户说可以”只在当前会话正在等待关联名单确认且没有同时修改时才能构成确认；设计已规定，但当前 `wait_for_selection_confirmation` 和 confirm 路由尚未实现。
- 当前没有 actor/brand 权限，所以不能在 adapter 中只依赖 prompt 或模型判断数据范围。
- 当前日志配置没有实现，后续可能把 Bearer token、完整请求体、完整推荐理由或敏感业务数据写入日志。

**结论：设计已有正确边界，代码尚未覆盖；子阶段 2–4 必须把边界落实到 adapter、typed result、context builder 和确认节点。**

**建议处理：**

- 所有外部文本携带 `source=java_api/user_message/tool_result` 等来源，不进入高优先级指令区。
- Adapter 层剥离认证 header、内部 URL、数据库错误和完整敏感响应；模型只接收有界、脱敏、结构化摘要。
- 高风险操作仍由确定性节点做权限、状态、revision、snapshot 和 confirmation 校验，不能由模型决定“现在应该提交”。
- 日志记录 operation/request/confirmation ID、状态和 hash，默认不记录 token、完整请求体和未经脱敏的结果正文。

### 3.7 其他直接相关的 Agent 问题

#### 中断、恢复和图路由一致性

**结论：设计已考虑，当前实现尚未覆盖；其中有一项后续实现前应修正。**

- 设计明确要求 `interrupt()`、`Command(resume=...)`、独占 thread、重授权和 checkpoint/业务台账对账。
- 当前 `wait_for_user` 直接抛异常，图中 `wait_for_user → prepare_context` 只是静态边，不代表可恢复中断。
- 当前 `wait_for_selection_confirmation → END`，没有把选择编辑、`selection_patch`、最终确认和 reserve/submit 串起来。
- `route_after_validation` 只覆盖 wait/operation/end，图中声明的 recommendation/review 路由尚未可达；这是子阶段 1 后续实现范围，但接入 adapter 前不能把当前 graph 当成完整主流程。
- `prepare_context` 当前只返回版本，不加载 actor、brand、业务对象或记忆；恢复时会缺少可信上下文。

建议子阶段 5 持久化前先定义一次“等待用户”的状态协议，避免后续把 `WorkflowNotImplemented` 替换成多个不兼容的 resume 方式。

#### 重复消息、重复工具调用和副作用去重

**结论：设计已考虑，当前未实现。**

- 设计已有 `client_event_id`、event content hash、operation/request/confirmation ID 和 generation 规则。
- 当前 `applied_event_id` 只是 AgentState 字段，没有仓库唯一约束，也没有同 ID 同内容返回原凭据/异内容拒绝逻辑。
- 当前 `WorkflowService.process()` 直接 `graph.ainvoke()`，没有持久化事件受理、租约、串行消费或外部 request 对账。
- 设计建议的去重逻辑不能只放在 graph state，因为 checkpoint 与业务事务不是原子提交。

应在子阶段 2 adapter contract 中先保留 `operation_id/request_id/idempotency_key` 和 source event ID；持久化、唯一约束和真正去重放到子阶段 5。

#### 超时、取消、轮询和 unknown

**结论：设计和业务索引已考虑，当前未实现。**

- Java 业务代码已说明推荐启动会 create + execute 下游任务，之后由 `RecommendationPollingJobHandler` 每 30 秒查询 progress/result。
- Java Retrofit 超时、下游 create 成功但 execute 或响应超时、Java 本地状态已写但响应丢失等情况需要区分 unknown，不能自动重新 create。
- 当前 `BusinessServices` 只有裸 create 方法，没有 timeout、error、unknown、poll 和 reconcile 类型。
- 取消外部推荐是否真实取消尚未有 Java 契约；设计要求只能显示停止等待，不能宣称外部任务已终止。

子阶段 2 应先定义 typed error/operation 结果；子阶段 3 再实现轮询和 unknown 对账。

#### 模型输出失败与降级

**结论：设计已考虑，当前没有模型实现。**

- 设计要求结构化 schema 无效时不执行部分 patch，有限重试后保留原表单并明确错误。
- 当前没有模型客户端、schema repair、超时、token 预算或降级模型配置。
- LLM 只应负责抽取、澄清和解释；结构化 patch 仍要经过 Pydantic 和确定性业务校验。

接入 LLM 前需要明确供应商 API、JSON schema 支持、超时/重试、模型版本和不可用时的手动表单路径；不能把模型调用失败当成空 patch 或默认值。

#### 日志、审计和可观测性

**结论：设计已考虑，当前代码只有基础入口，没有实现。**

- 技术文档 §9 已规定 trace/conversation/task/request、节点耗时、模型/prompt/schema 版本、revision/hash、外部状态、memory 和 Outbox 重试。
- 当前 `config.py` 只有 environment，`main.py` 只装配 WorkflowService，没有数据库、模型客户端或日志脱敏配置。
- 子阶段 2 至少需要让 adapter 结果和错误带 operation/request/trace 关联，但不应将 secret 和完整业务响应写日志。

## 4. 设计约束与建议

### 4.1 进入子阶段 2 前必须明确的实现契约

第 9 节已确认：业务写入统一经 Java SmartProposal；`batch-select` 保存用户编辑、`batch-confirm-selection` 作为最终名单确认入口；Campaign/Task/recommend 写入前都要用户明确确认；品牌只能来自当前用户可选品牌列表；模型不直接调用业务工具。

仍需外部契约落地：

1. Java 服务真实部署地址/代理路径，以及 Agent 服务到 Java 的委托身份传递方式（保留 Venus OAuth/`SecurityUtils` 用户），不能把未签收的自定义 header 当成已具备能力。
2. Agent 服务端读取当前用户品牌列表的认证方式、响应结构、撤权/缓存行为及品牌 ID/code/name 权威映射。
3. Java create 是否接受 operation/idempotency key、按 request ID 查询和 unknown 对账；目前源码快照未见相关接口。
4. Java HTTP 状态、真实 `BaseResponseEntity` 错误 envelope、业务 code 与网关错误映射。
5. 前端/Agent API 如何提交绑定当前预览的结构化确认事件。
6. `FormPatchEvent.task_id` 与 workflow task 归属、`set null`/`clear`/`use_default` 语义（本地已实现并测试）。

### 4.2 接入 LLM 自然语言抽取前必须完成

- 标准消息格式转换：业务消息档案、工作消息窗口、tool result 和结构化事件分开。
- Context Builder 的固定 section、动态 section、长度预算和摘要/压缩协议。
- 结构化抽取 schema：只允许当前阶段白名单字段、patch operation、ambiguities 和 evidence，不允许输出工具 URL、身份、路由或确认状态。
- 模型失败、超时、schema 修复失败和无模型时的手动表单路径。
- 提示注入数据边界和日志脱敏规则。

### 4.3 子阶段 2–4 的 adapter/工具约束

- 使用 typed request/response/error/operation models，不返回裸 `int | str` 或任意 dict 作为核心跨模块契约。
- adapter 只做字段映射、传输、响应 envelope 解析和错误分类，不夹带品牌推断、默认值推断或“是否应该确认”的模型判断。
- 每个有副作用调用带 operation/request/confirmation 标识、canonical payload hash、actor scope 和当前 revision。
- 将 create Campaign、create Task、start recommendation、read status/results、普通 select、最终 confirm 分成独立能力，分别标注只读/异步/持久化/封板副作用。
- 外部结果分为机器结构和有界模型摘要；完整结果通过引用或业务结果接口读取。
- adapter 错误中不能包含 Bearer token、内部 URL、完整 SQL/堆栈或未经脱敏的业务响应。

### 4.4 子阶段 5 持久化、恢复和鉴权阶段验证

- `conversation_messages` 保存完整可审计历史，AgentState 只保存可恢复工作窗口和结构化事实。
- checkpoint 往返序列化后重新读取业务表和事件台账，验证 Pydantic 类型、schema_version 和旧 thread 迁移。
- event client ID、source event ID、operation/request/confirmation 唯一约束和同一 conversation 串行锁。
- interrupt/resume、SSE 补拉、进程重启、旧权限撤销、brand 切换和旧 pending UI 恢复。
- tool/API 原始结果不会被误当用户消息或系统指令。

### 4.5 第一阶段整体联调前验证

- 超长上下文、摘要压缩、超长工具输出和关键事实交叉校验。
- 参数保真 contract tests 和 Java UAT create/task/recommend/results/confirm 联调。
- 提示注入测试、未确认不得封板、过时 revision/snapshot 拒绝、重复/unknown 对账。
- 推荐理由、KOL 描述、错误文本和历史消息中的指令不改变权限或路由。
- 供应商 KV Cache 支持和收益实测；没有供应商证据前只作为优化假设。

### 4.6 只需保留扩展空间或当前不需要处理

- 通用 Shell/命令解析器、文件系统 sandbox、浏览器工具：第一阶段没有此类能力，当前不适用。
- KV Cache 专项优化：保留稳定 prompt/message/tool schema 的结构，但不增加缓存专用抽象，也不以缓存为正确性前提。
- 多 Agent、插件系统、任意 MCP 工具注册：设计已排除，当前不引入。
- MemU 或其他语义记忆组件：当前第一阶段仍以结构化 PostgreSQL 记忆为事实源，属于独立 PoC，不阻塞 Java adapter。

## 5. 对现有方案和 TODO 的影响

### 子阶段 1 调整范围

不建议回滚或重做子阶段 1。拆分后的领域模型、`AgentWorkflowStage`/`BusinessTaskStatus` 分离、结构化 patch 和 revision 适合作为后续基础。

本轮已完成以下小范围修正：

- 在 `apply_form_patch()` 增加 event task ID 与 draft task ID 的归属校验。
- 明确 `set null`、`clear`、`use_default` 的差异，并为 `use_default` 定义是否实际解析默认值和是否递增 revision。

`use_default` 仍没有外部默认值解析 provider，因此当前只清除显式覆盖并解除 suppression，不会生成默认值。子阶段 1 其他结构没有返工。

### 对 AgentState、消息结构和 Java adapter 的影响

- `AgentState` 需要保留业务草稿，但后续应增加标准消息工作窗口、摘要覆盖水位、当前阶段、operation/confirmation 关联和可恢复 pending UI；完整聊天消息不应只存在于 AgentState。
- `incoming_event` 适合确定性事件入口，但不能替代 `messages`。LLM 接入时需要标准消息转换层。
- Java adapter 已从裸 `int | str` 扩展为 typed Campaign/Task request、response、error、operation 和 unknown，保留 actor、operation、revision、payload hash、confirmation 与可信品牌引用。真实 Java identity/idempotency/reconciliation 契约仍未核实，写 gate 默认关闭。
- 不需要因为 KV Cache 立即改变 AgentState，也不需要把所有动态状态塞进稳定 prompt 前缀。

### 对后续子阶段顺序和完成标准的影响

原计划总体顺序仍然合理，但建议在子阶段 2 内部先增加一个短的“adapter contract gate”：

1. 定义 typed request/response/error/operation 模型。
2. 完成只读 Campaign/Task 查询和纯映射测试。
3. 确认认证、幂等和 unknown 方案后，才启用真实 create/task 副作用。

这不是新增第一阶段范围，也不是推迟 Java adapter；它是避免先写错误副作用调用的实现顺序调整。

建议子阶段 3 的完成标准增加：超长结果有界投影、原始结果引用和错误摘要不会无限进入模型上下文。

建议子阶段 4 的完成标准增加：用户确认来源必须是当前关联名单卡的结构化事件，不能由模型总结或工具返回文本代替。

建议子阶段 5 的完成标准增加：checkpoint、消息历史、业务台账和压缩摘要恢复后，revision/operation/confirmation 仍一致。

### 只是优化建议、不阻塞当前开发的事项

- KV Cache 前缀稳定性和缓存命中率优化。
- 选择更复杂的摘要算法或专用记忆组件。
- 工具结果的更高级自然语言摘要。
- 细粒度模型成本优化和多模型路由。

本轮已将实际实施进度和未决外部契约同步记录到 [第一阶段内部实施子阶段TODO.md](第一阶段内部实施子阶段TODO.md)；子阶段 2 整体保持未完成。

## 6. 第 9 节决策与外部契约状态

以下方向已由第 9 节确认，不再作为开放产品问题；外部系统能力仍须契约或联调验证。

### 业务/架构

1. 已确认所有业务写操作经 Java SmartProposal，Agent 不直调 Python 下游或业务数据库；Java 部署地址及委托身份认证待核实。
2. 已确认超时按 operation/request ID 查询，结果不明时保留 unknown 且不自动重试；Java 是否提供幂等和查询/对账接口待核实。
3. 已确认 `batch-select` 是持久化编辑，`batch-confirm-selection` 是最终封板候选入口；回执、重复提交和 revision/snapshot 契约待核实。
4. 已确认品牌来自当前用户可选品牌列表；Agent 后端认证、响应结构及品牌 ID/code/name 映射待核实。
5. 已确认 Campaign create、Task create、recommend 启动均需写前明确确认；前端/Agent API 当前预览绑定事件待实现。

### 模型/平台

1. 已确认将 prefix/KV cache 支持列为供应商要求，但不作为正确性前提；缓存命中条件、计费、TTL、跨请求范围待供应商核实。
2. 已确认优先 structured output，失败走有限重试与人工补充路径；目标模型 API/schema 能力待选型。
3. 已确认模型不直接调用业务工具，确定性 LangGraph 节点负责授权校验和 adapter 调用。

### 前端/QA

1. 已确认最终名单确认使用结构化 `selection.confirm` 并绑定 campaign/task/revision/snapshot/selected IDs；Java 当前是否提供 revision/snapshot 字段待核实，Agent 前端事件待实现。
2. 状态展示方向已确认；长文本、工具错误、unknown、分页与恢复 UI 协议尚未实现。
3. 上下文压缩验收关键事实已确定，QA 用例在后续相应子阶段落实。

## 7. 建议的后续验证清单

| 验证项 | 建议子阶段 | 目标 |
|---|---|---|
| 事件 task ID 与 workflow task ID 归属 | 子阶段 2 前/子阶段 2 | 防止事件修改错误 Agent workflow |
| `set null`、`clear`、`use_default`、未提供字段 | 子阶段 2 | 固定参数和默认值语义 |
| Java request/response/error contract | 子阶段 2 | 验证字段、空值、金额、日期、ID、envelope 和错误分类 |
| create/task/recommend 超时与 unknown | 子阶段 2–3 | 防止重复创建或重复启动 |
| 不同 operation 的 typed ACI 结果 | 子阶段 2–3 | 区分查询、创建、启动、圈选和封板 |
| 上下文超限和结构化压缩 | 子阶段 3/5 | 验证关键事实、revision、状态和确认不丢失 |
| 消息只增不改、标准消息格式和 tool call 配对 | LLM 接入前/子阶段 5 | 保证恢复、审计和模型上下文正确 |
| 超长 API/工具结果有界投影 | 子阶段 3 | 防止模型上下文失控和敏感信息扩散 |
| 提示注入测试 | 子阶段 3–4 | 用户、Java 返回、推荐理由、错误文本不能改变权限或路由 |
| 未确认不得执行最终封板 | 子阶段 4 | 验证工具/模型不能替代用户确认 |
| snapshot/revision 过时拒绝 | 子阶段 4–5 | 防止旧名单覆盖当前选择 |
| 重复事件、重复工具调用、重复确认 | 子阶段 5 | 验证 event/operation/request/confirmation 幂等 |
| interrupt/resume、重启、SSE 断线和摘要恢复 | 子阶段 5 | 验证 checkpoint、消息、业务台账一致 |
| KV Cache 供应商能力和实际命中 | 整体联调 | 只验证优化收益，不作为正确性前提 |
| 无 Shell 能力边界回归 | 整体联调 | 确认没有意外暴露通用命令或任意工具 |

## 8. 复盘结论

当前设计能够支撑继续开发，子阶段 1 不需要推翻。第 9 节记录了已确认的业务/架构、模型/平台及前端/QA 决策；其中涉及外部服务能力的事项仍须以 Java、模型供应商和前端的契约或联调核实。

上下文管理、消息历史、interrupt/resume、持久化、可信鉴权及提示注入防护仍主要处于后续实现阶段。子阶段 2 已落地本地 typed ACI、参数映射和业务写入双确认门控，但真实 Java 身份、品牌、幂等与 unknown 对账契约尚未验证，真实写 gate 保持关闭。KV Cache 是已确认的供应商能力要求，命中条件、计费、TTL、跨请求范围和工具 schema 稳定性仍待验证，且不能作为正确性前提。Shell/通用命令危险操作不适用于当前第一阶段。

## 9. 待确认问题的答复与设计建议

本节记录业务/架构、模型/平台、前端/QA 待确认问题的答复，以及对上下文管理方案的评估。这里的“已确认”表示本轮讨论中形成的产品/架构决策；涉及 Java 服务端、模型供应商或前端接口的能力，仍需在对应契约和联调中核实，不代表外部系统已经具备该能力。

### 9.1 业务操作、幂等与用户确认

1. **业务写操作统一经过 Java SmartProposal API。** Agent 不直接调用 Python 下游服务，也不直接访问业务数据库。Java adapter 是业务操作的受控边界，负责可信身份、品牌授权、参数校验、错误映射和审计。所有会改变业务状态的调用由确定性的 LangGraph 流程节点发起。
2. **超时结果必须可对账，不能盲目重试。** 期望 Java 提供 operation/idempotency key、按客户端请求 ID 查询，以及对未知结果进行对账的能力。Campaign、Task 创建或 recommend 启动超时后，先按请求 ID 查询最终状态；无法确认时保持 `unknown` 并等待对账/人工处理，不自动再次创建或启动。该能力需在子阶段 2–3 的 Java 契约中确认。
3. **名单编辑和最终封板分为两步。** `batch-select` 用于保存用户编辑中的选择；`batch-confirm-selection` 作为 Agent 最终名单确认入口。只有用户明确确认后才能封板，不能把普通选择、模型推断或工具返回文本当成确认。前端现有确认页已分别提供保存编辑和确认提交操作，且使用 `batch-select` 与 `batch-confirm-selection`；Agent 事件应保持同样的语义区分。
4. **品牌取自当前用户可选品牌列表。** 使用用户提供的接口 `https://smart-social-uat.platform-loreal.cn/api/brand/list-by-current-user` 获取品牌候选，并在可信用户上下文中校验选择。Agent 不以用户随口输入的品牌名称作为授权依据；品牌缺失或有歧义时展示可选品牌并要求用户选择。前端当前代码已有 `getBrandList`，其配置 URL 由 `VITE_API_SOCIAL_MEDIA_BASE_URL` 与 `/brand/list-by-current-user` 组成；Agent 服务端接入时仍需确认正式环境地址、响应结构和鉴权传递方式。
5. **所有业务写入/启动操作前统一向用户确认。** Campaign create、Task create、recommend 启动等会产生业务副作用的操作，在执行前向用户呈现将执行的操作及关键参数并取得明确确认。字段完整只代表可以进入确认步骤，不等于用户已经授权执行。

### 9.2 模型与工具调用

1. **将 prefix/KV cache 支持列为模型供应商要求。** 这是性能和费用优化能力，不是正确性前提。选型和联调时需核对缓存命中条件、计费、TTL、跨请求范围及工具 schema 稳定性，并用实际请求验证命中情况；即使缓存不可用，功能和安全语义也必须保持正确。
2. **要求结构化输出并提供安全兜底。** 优先使用 Pydantic/JSON schema structured output。遇到 schema 错误或短暂超时，可做有限次数重试；仍失败或模型不可用时，进入可恢复的错误/人工补充路径，不根据不完整或无法校验的输出执行写操作。备用模型只有在供应商、数据处理和费用策略明确后再启用。
3. **模型不直接决定并执行业务工具调用。** 模型负责理解用户意图、抽取字段和生成受 schema 约束的结果；确定性的 LangGraph 节点负责校验权限、确认状态、路由和调用 Java adapter。这样能把模型输出限制在数据输入层，避免模型绕过确认、授权或幂等流程。

### 9.3 前端确认事件与状态展示

1. **最终名单确认使用结构化事件。** 前端现有确认页分为编辑保存和最终提交两个操作：保存调用 `batch-select`，定稿调用 `batch-confirm-selection`。Agent 接口建议将其表达为独立的 `selection.confirm` 事件，并绑定 Campaign、Task、名单版本、候选快照和明确选中的 KOL ID。建议事件载荷如下，具体字段名需与前后端协议统一：

   ```json
   {
     "type": "selection.confirm",
     "campaign_id": "123",
     "task_id": "456",
     "selection_revision": 7,
     "candidate_snapshot_ref": "snapshot-abc",
     "selected_kol_ids": ["kol-1", "kol-2"]
   }
   ```

   前端当前 `batch-confirm-selection` 请求已经按 task 传递 KOL 选择项，但本次检查的代码中未看到 `selection_revision` 或候选 snapshot 字段。Agent 侧不应伪造这些版本值；需要 Java/业务后端提供或确认对应字段及校验能力。版本或 snapshot 已过期时应拒绝此次确认、返回当前名单状态并要求用户重新审阅确认。确认必须来自当前关联名单卡的显式结构化事件，普通聊天文本、模型总结及工具结果均不能代替。
2. **定义统一的前端状态规范。** 至少覆盖进行中、成功终态、失败、结果未知、待用户确认、候选分页、断线重连/恢复等状态；明确每种状态的文案、可执行操作和是否允许重试。尤其 `unknown` 不能展示成成功，也不能鼓励用户直接重复提交；恢复后须显示当前 operation 状态和待处理动作。

### 9.4 上下文管理和压缩建议

采用用户提出的分层方案，并将“上下文压缩”定义为控制模型本轮可见内容，而不是缩短或覆盖业务事实。核心原则是：完整状态用于恢复与审计，Context Builder 按白名单挑选本轮所需内容。

1. **L1：白名单上下文组装。** 保留完整 workflow state、表单状态、业务台账和消息历史；每轮只将当前任务必要字段、近期对话、未解决事项、可信来源引用等组装进模型上下文。通过有界投影和大对象引用，减少噪声进入模型。该层优先实现，通常不需要 LLM 调用。
2. **L2：结构化摘要。** 仅在工作消息超过约 6k token，或发生 `CAMPAIGN_CREATED`、`CANDIDATE_REVIEW` 等阶段切换时触发，不要求每轮摘要。6k 是待验证的初始阈值，应通过目标模型的可插拔 token 估算和实际上下文预算校准，不绑定某个 tokenizer。摘要使用可校验 JSON，至少包含 `task_id`、`decisions`、`rejected_options`、`unresolved`、`source_message_ids`、`covered_until_message_id`；用 `summary_covered_until_message_id` 做幂等标记，避免重复摘要。金额、否定条件、revision、Task ID、用户确认和未完成操作等关键事实须与表单/业务状态交叉校验，LLM 生成的摘要不能覆盖结构化事实。
3. **摘要可回溯，保留原始证据。** `source_message_ids` 应能定位原始消息；`covered_until_message_id` 表示摘要覆盖边界。Checkpoint 负责恢复图状态，Context Builder 负责决定本轮模型读取什么，两者职责分开。第一版可只在组装层隐藏历史消息，不必删除 checkpoint 或原始消息。若后续确需在 LangGraph `add_messages` 状态中移除旧消息，必须显式使用 `RemoveMessage`，并针对 reducer 行为、覆盖边界和重复摘要添加测试。
4. **L3：隔离大结果与候选池。** 不把完整候选池或大型 API 结果一次性放入提示词。候选按每批约 50–100 个进行裁剪后评估，并记录批次版本、覆盖数和评估进度，最终由确定性代码聚合。大型候选集和结果通过现有 `candidate_snapshot_ref`/`result_ref` 外置；上下文只保存引用、分页元数据和有限预览，需要时再通过受控工具读取。
5. **最小实施顺序。** 先实现 state 与白名单 Context Builder；随后实现阶段触发、约 6k token 初始阈值和结构化摘要；最后完善大结果外置引用、分页回读及批次覆盖记录。阈值和批次大小均为初始设计参数，应在真实负载和模型上下文限制下验证。
6. **验收案例。** 至少测试金额、否定条件、旧确认、Task ID 和未完成任务在组装、摘要、重启恢复后的保留与语义；同时核对 revision、来源消息引用和摘要覆盖边界。旧确认不得被解释成当前确认，未完成的写操作不得因摘要而被误报为已完成。
7. **不采用的做法。** 不用滑动窗口直接丢弃仍影响任务语义的消息；不每轮都压缩；不以无来源的散文摘要代替结构化状态；不把大量候选直接交给模型做一次性选择；第一阶段不为精确匹配记忆引入向量库。模型 token 估算应保持可插拔，避免在目标模型未确定时硬编码 tokenizer。

以上决定需分别转化为子阶段 2–5 的 API 契约、LangGraph 状态/节点、前端事件协议和验收用例；本节中的建议参数（例如 6k token 和每批 50–100 个候选）需经联调验证后再固化。

### 9.5 子阶段 2 落地状态（2026-11-19）

本小节只记录实现进度，不改写 9.1–9.4 的已确认产品/架构决策。

- **已落实到本地代码：**所有 Campaign/Task 写入需显式结构化确认；确认绑定当前 Agent task、可信 actor scope、operation、form revision、canonical payload hash 和 confirmation ID。Campaign create 与 Task create 使用独立预览和确认；已有 Campaign 选择不构成授权，Task create 前执行 Campaign detail 读取并校验品牌上下文。
- **已实现 Adapter contract gate：**Pydantic typed Campaign/Task 请求、结果、Java envelope、错误分类、operation receipt/unknown、Decimal/date/camelCase 映射和 canonical hash。adapter 仅字段映射/传输/响应解析，不推断身份、品牌、确认或默认值；写操作不自动重试。
- **安全默认：**部署 base URL、Bearer token 和 Java owner 签收的身份、operation/request/idempotency/confirmation/revision/hash header 与能力 flags 不齐全时，写请求在网络调用前被拒绝。Java 本机源码尚未证明这些 header/idempotency 查询能力存在；配置 header 本身不表示外部支持。
- **品牌边界：**Campaign create 的 `brandName` 只能由可信 `TrustedBrand` 提供，且必须匹配 actor brand ID；已有 Campaign 还要求 detail 中 brand name/code 匹配可信目录项。前端使用的品牌服务候选结构（页面读取 `code/name`）已查到，但 Agent 后端的服务端认证、授权撤权和映射契约仍待确认。Java detail `brandCode` 的实际序列化也需要测试服务确认。
- **参数语义：**`set null` 拒绝；`clear` 置空并屏蔽默认；`use_default` 清除显式覆盖并解除 suppression，但不创建默认服务、不猜测默认值；无默认 provider 时字段保持未设置并继续接受 required validation。未提供字段在 adapter JSON 中省略，空字符串是显式字段值并交给 Java DTO 校验。
- **子阶段 1 小范围修正：**补充事件 task ID/workflow task ID 归属检查；按 create 阶段拆分 Campaign 与 Task 必填校验；修正 Campaign 创建后接续 Task 阶段的本地图路由。没有重做子阶段 1。
- **本地验证：**`.venv/bin/python -m pytest -q` 为 28 passed；`.venv/bin/python -m compileall -q app tests` 和 `git diff --check` 通过。覆盖纯映射、错误 envelope、timeout/408/409/429/5xx unknown 与 no-retry、缺少 contract gate 时零请求、双确认和品牌不匹配阻断。
- **仍未验证：**真实 SmartProposal base path/部署路由；Agent→Java 委托身份如何保留 Venus OAuth `SecurityUtils` actor；当前用户授权品牌列表后端响应及 code/name/brandCode 映射；HTTP/business error 的部署实况；服务端接收幂等 operation/request ID 与超时查询/unknown 对账；Java detail brandCode 序列化；测试服务或真实 UAT 的 Campaign create 和 Task create。当前没有执行任何真实写请求。
- **阶段状态：**子阶段 2 只完成本地 contract gate、实现和测试，不满足 TODO 的测试服务/UAT 新 Campaign→Task、已有 Campaign→Task 与重复/超时不重复创建验收；子阶段 2 仍未完成。完整 operation ledger 仍属子阶段 5，不在本轮提前实现。

### 9.6 子阶段 3 落地状态（2026-11-19）

本小节记录推荐启动、Task 状态、结果投影和恢复决策的本地实现，不把本地测试写成真实服务验收。

- **typed contract：**增加 Java recommend 无 body start、Task list/detail、results、run status、poll event、snapshot reference、candidate projection 和 recovery decision 模型。Java 的结果仍按 `summary + kols[]` 处理，未额外假定分页、全集或稳定 snapshot 字段。
- **独立确认与副作用边界：**`recommendation.start_requested` 只生成 preview 并读取 Task；`recommendation.confirm` 携带独立 confirmation/operation/request/revision/hash，确认后再读 pending Task，adapter 只发一次 recommend POST。Task create confirmation 不可授权 recommend。
- **状态和 unknown：**`accepted` 只表示 Java 受理；Task 状态映射为 pending/in_progress/recommend_completed/failed/ended，未知值进入 unknown。poll sequence 防止旧事件覆盖新状态；写超时或结果不明不自动重启，旧 pending Task 也不能证明写入未发生。
- **结果和上下文边界：**只在 `recommend_completed` 且结果关联校验通过后读取 results；adapter 限制响应大小/候选数量，投影只保留最多 20 条安全字段，理由截断和 URL 协议过滤，完整结果通过引用保存。`candidate_universe_complete=false` 明确表示没有候选全集契约。
- **恢复实现边界：**增加可序列化恢复决策（待确认、单次启动、查询 Task、读取结果、人工对账、停止）和进程内 volatile snapshot store。跨进程重启、持久化快照、事件去重、worker、租约、Outbox、SSE 和 checkpoint 仍保留到子阶段 5。
- **本地验证：**`.venv/bin/python -m pytest -q` 为 40 passed；compileall 和 `git diff --check` 通过。覆盖 bodyless POST、双确认隔离、受理/完成分离、unknown/no-retry、重复/乱序 poll、results 限制、安全投影、snapshot 引用和图级 preview/confirm/poll。
- **外部边界：**尚未验证 Java 目标部署路径、Venus OAuth 委托身份、品牌/tenant 传递、recommend 服务端幂等与按 request ID 对账、results 候选全集/分页/稳定快照和真实 UAT 链路；本轮没有真实 recommend 写请求。
- **阶段状态：**子阶段 3 只完成本地实现，未满足 TODO 的真实联调、结果完整性契约和 durable 恢复验收；子阶段 3 整体暂不标记完成。本轮停止，不进入子阶段 4。

## 10. 子阶段 3 后第二轮 Agent 专项复盘（2026-11-19）

本章保留首轮复盘和第 9 节的历史含义，只记录在子阶段 2、3 本地代码出现后重新检查得到的事实、边界和建议。建议不自动成为新的产品或架构决策；涉及 Java、前端、业务 owner 或模型平台的事项，仍须由对应负责人签收。

### 10.1 复盘基线

当前真实状态如下：

- 子阶段 2 已有本地 Campaign/Task typed adapter、contract gate、Campaign/Task operation preview 和独立确认流程；真实 Java/UAT 身份、品牌、幂等、请求 ID 对账和写入链路仍未验收，真实写 gate 默认关闭。
- 子阶段 3 已有本地 Recommend preview、独立确认、Task detail/list typed read、单次无请求体 recommend POST、状态轮询、unknown/有限读取失败决策、结果读取、受限投影和进程内 volatile snapshot store；真实 Recommend 联调、结果全集/分页/稳定快照契约和 durable 恢复仍未验收。
- 本轮没有发送真实 Recommend、`batch-select` 或 `batch-confirm-selection` 写请求，也没有修改业务代码、测试、TODO、CODE_INDEX 或 BUSINESS_CODE_INDEX；只更新本复盘文档。
- 本轮重新阅读了实施计划、技术实现文档、本复盘、业务代码索引、代码索引、内部子阶段 TODO、AgentState/WorkflowDraft/domain/events/recommendation schema、draft/recommendation service、SmartProposal adapter、snapshot store、contracts、全部相关本地测试，以及 Java `TaskController`、`RecommendationPollingJobHandler`、`SelectionConfirmServiceImpl` 和 Selection DTO 源码。
- 当前实际本地验证为 `.venv/bin/python -m pytest -q`：**40 passed**；`.venv/bin/python -m compileall -q app tests` 通过；`git diff --check` 通过。该结果只证明当前本地代码和 Mock/本地流程，不证明 Java/UAT 联调。

本轮没有覆盖真实模型消息格式、模型供应商缓存计费、PostgreSQL/checkpointer、事件台账、worker/lease、SSE、前端实际名单编辑调用、Java 部署身份链路、真实 selection 回执和真实结果分页。因此下文对这些部分只做代码/源码边界判断，不写成已验证能力。

### 10.2 首轮建议落实矩阵

| 首轮建议或已确认决策 | 当前实现位置与证据 | 当前状态 | 下一步时机 |
|---|---|---|---|
| 业务写入统一经过 Java SmartProposal，Agent 不直调 Python 或业务库 | `BusinessServices`、`SmartProposalAdapter`、recommendation nodes；本地节点只调用 typed service | **已落地到本地边界，只有本地验证** | 子阶段 2/3 真实联调时验证部署路径和身份；整体联调验证无旁路 |
| Campaign/Task/recommend 各自绑定独立 operation、request、confirmation、revision/hash | `PendingBusinessOperation`、`RecommendationRun`、`RecommendationOperationConfirmation`、canonical payload hash；测试覆盖 Campaign/Task 双确认和 recommend 独立确认 | **部分落地**：本地模型和节点已有；持久化唯一约束、同事件去重和跨进程提交点没有 | 子阶段 5 落地 durable ledger；子阶段 4 沿用同一模式 |
| Recommend 启动必须独立预览和明确确认，Task create 确认不能授权 Recommend | `prepare_recommendation_preview`、`confirm_recommendation`、`execute_recommendation_operation`；`test_graph_keeps_recommend_preview_and_start_as_two_separate_events` | **已在本地代码和测试覆盖** | 子阶段 4 的 selection save/confirm 采用同样的独立事件，不复用 recommend confirmation |
| Recommend 受理不等于完成 | `RecommendationStartAccepted`、`RecommendationRunStatus.ACCEPTED`、poll 后才进入 `RECOMMEND_COMPLETED`；测试覆盖 accepted 不可再次 start | **已在本地代码和测试覆盖** | 真实联调核对 Java HTTP/business code 与任务状态转换 |
| unknown 不自动重启，先按 request/operation 对账 | `OperationReceipt.UNKNOWN`、`write_outcome_unknown`、`apply_task_poll` 在 unknown 时保持 `RECONCILIATION_REQUIRED`；恢复决策返回人工对账 | **本地状态规则已覆盖，外部对账未实现** | 子阶段 2/3 Java owner 确认 request ID 查询；子阶段 5 做 durable recovery |
| Java Task 是业务状态权威，保存 `downstreamTaskId`，不另造并行推荐事实 | `JavaTask`、`RecommendationRun.downstream_task_id`、Task status mapping 和结果读取 request | **部分落地**：字段关联有；AgentState top-level run 与 `WorkflowDraft.recommendation_run` 存在重复事实，尚无一致性约束 | 子阶段 4 前应明确单一状态 owner；子阶段 5 从业务台账重建 checkpoint |
| 大结果不进入模型上下文，保留有界投影和引用 | adapter 结果大小/候选数限制；`CandidatePreview` 最多 20 条；理由截断、URL 过滤；volatile store 通过 ref 保存原始 typed 结果 | **本地有限实现** | LLM 接入前补 Context Builder、字段白名单、来源标记和引用读取策略；候选分页在子阶段 4 |
| 候选全集不能由首屏或 `shortlistCount` 推断 | `candidate_universe_complete=False`；`BUSINESS_CODE_INDEX` 和结果 schema 均保留未确认状态 | **设计约束已落实到标记，但模型语义仍需收紧** | 子阶段 4 contract gate 前确认 total/cursor/full snapshot；此前不显示“全部候选”或允许最终封板 |
| 确认只能由确定性结构化事件产生，模型不能直接执行工具 | 当前图的 Recommend 事件分流和确定性节点；没有通用 URL/token/path/JSON 工具 | **已落地到当前本地能力；名单确认尚未实现** | 子阶段 4 定义 `selection_patch` 与 `confirm_selection` typed events |
| batch-select 与 batch-confirm-selection 语义分开 | Java `TaskController` 和 `SelectionConfirmServiceImpl` 源码明确：前者持久化选择/取消/拒绝，后者事务内先选择再封板；当前 Agent 没有对应 adapter | **外部源码事实已确认，Agent 代码未实现** | 子阶段 4 本地先实现草稿/typed mock；真实写入等 Java request/revision/幂等契约 |
| checkpoint、消息历史、事件去重、租约、Outbox、SSE 不提前伪造 | 当前仍无数据库、持久化 snapshot、worker、SSE；文档和 TODO 保留到子阶段 5 | **设计范围保持正确** | 子阶段 5 |
| Shell、文件系统、浏览器、任意命令能力不引入 | 当前仓库没有这些工具接口 | **不适用，保持现状** | 不增加通用命令解析或 sandbox |

首轮有关“Campaign 是否增加 Agent 专属确认”的历史文档存在两种表述：技术文档第 1.3、2.2 仍写“Campaign 创建不增加 Agent 专属确认”，而首轮第 9 节及当前本地代码采用了 Campaign、Task、Recommend 分别确认的较新决策。本轮不擅自选择第三种规则；代码事实是当前 Campaign create 有确认门，外部业务契约是否要求或允许该门仍需在决策记录中统一表述。该差异不直接阻塞子阶段 4 的 selection 草稿，但会阻塞完整流程的最终产品/接口签收。

### 10.3 AgentState、业务事实与上下文管理

当前职责分层已经有基础，但还没有达到可恢复业务事实的要求：

- `WorkflowDraft` 保存 Campaign/Task 草稿、form revision、业务 ID、pending operation、recommendation run、候选 snapshot/result 引用和 selection draft；`RecommendationRun` 保存 operation/request/downstream ID、状态、poll sequence、结果 hash 和 reconciliation flags。这个方向符合“业务事实与模型上下文分开”的首轮决策。
- `CandidateProjection.items` 只有最多 20 条 `CandidatePreview`。`total_count` 当前是本次 Java `kols[]` 响应的数量，不是已经确认的候选全集总数；`response_complete=True` 只能理解为当前 HTTP 响应已完整读取，不能理解为候选全集完整；`candidate_universe_complete=False` 是唯一明确的全集阻断标记。若上层只读取 `total_count` 或 `response_complete`，仍有误判风险，建议子阶段 4 改用明确的 `scope=page|full_unknown|full_verified`、`returned_count`、`total_count`、`next_cursor` 或等价字段。
- `RecommendationSnapshotReference.snapshot_ref` 是 Agent 本地生成的 UUID，`snapshot_uri` 是指向 Java results 路径的逻辑 URI；volatile store 还在进程内保存一份 typed 结果。它不是 Java 提供的不可变版本，也不是服务端并发锁。当前 `storage_owner="java_smartproposal"` 与本地存储行为容易混淆，后续应区分“source owner”和“Agent storage/durability”，不能把本地 ref 当成 Java snapshot token。
- `AgentState` 同时有顶层 `recommendation_run`、`candidate_projection` 等字段，`WorkflowDraft` 又保存 run、snapshot/result ref。当前推荐节点主要使用 draft 内字段，代码没有统一 reducer 或 invariant 防止二者分叉。这是维护性风险，不是本地推荐状态测试失败；子阶段 4 前应明确顶层字段是输出投影还是删除重复状态，子阶段 5 以业务台账为唯一重建来源。
- `SelectionDraft` 当前只有 `revision`、字符串 `selected_kol_ids`、`recommendation_run_id` 和 snapshot hash；没有 `selection_preview_id`、candidate page/coverage、model-selected IDs、actor、business Campaign/Task、selection payload hash 或 pending save operation。它足以作为早期领域占位，不能作为最终封板台账。

后续消息/上下文应继续按四层分工：`conversation_messages` 保存完整只增不改档案；工作消息窗口只供当前模型调用；结构化摘要保存覆盖边界和来源消息；checkpoint 保存可恢复的图工作状态。压缩不能覆盖 `business_campaign_id`、`business_task_id`、Agent task ID、form/selection revision、operation/request/confirmation、downstreamTaskId、snapshot hash、待对账状态和未完成写操作。恢复时必须重新读取业务台账、当前 Task、结果版本和授权上下文，不能用模型摘要或旧 checkpoint 直接恢复写权限。

新增结果文本目前进入 `JavaCandidateDetail` 后只在投影中使用名称、tier、有限理由和 HTTPS URL；原始 `description`、metrics、pricing 等仍存在 typed results/volatile store。理由有 20,000 字段上限并投影截断 240 字符，但 description 等字段的单字段边界和日志/消息处理尚无运行实现。LLM 接入前应补字段白名单、单字段/总投影上限、`source=java_api` 数据标记和原始结果引用，避免推荐理由、KOL 描述或错误文本被转换成普通用户指令。

分类：业务与 projection 分离为**部分落地**；结果全集语义和 State 单一 owner 属于**建议调整**；消息历史、摘要、checkpoint、恢复对账属于**后续实现**；当前没有模型消息或上下文压缩实现，不能声称 KV Cache、标准 tool message 或提示注入测试已生效。

### 10.4 KV Cache、标准消息和 ACI 调用边界

当前没有模型客户端、标准消息列表、prompt builder、tool schema 或 KV cache 配置，仍没有真实缓存验证结论。新增结果未来应作为带 source/type/operation/request/snapshot 引用的结构化 tool result 或 Context Builder 数据块进入模型，不能写成普通 user message，也不能由 assistant 文本升级为 confirmation。

固定规则、schema 和安全约束应保持稳定前缀；当前 actor/brand、草稿 revision、pending UI、结果 projection、候选页和 selection state 放动态 context；完整结果由受控引用读取。业务消息和事件档案只增不改，摘要只控制本轮可见范围，不覆盖原始事实。KV Cache 仍只是供应商能力和成本优化项，不是子阶段 4 正确性前提，也不需要本轮改代码。

ACI 方面，当前已独立区分 Campaign create、Task create、Recommend start、Task read/list、results read；这些调用由确定性 graph node 调用 typed adapter，未发现通用 URL、token、任意 path 或任意 JSON 输入路径。`batch-select` 和 `batch-confirm-selection` 在 Java 侧也有明确不同的业务入口，但 Agent 尚无对应 typed contract。子阶段 4 至少需要独立定义：

1. `read_candidate_page`：只读，返回 page items、returned count、total/cursor/完整性声明和 result version（若服务提供）。
2. `save_selection`：明确是 Agent 草稿保存、Java 持久化 `batch-select`，还是二者不同事件；返回 selection revision/current selection/operation receipt。
3. `confirm_selection`：只接受当前候选快照、selection revision、selected IDs 和独立 confirmation，调用事务性 `batch-confirm-selection` 或已签收的正式入口。
4. 每个写请求的 accepted/succeeded/rejected/unknown/reconciliation response、request ID、payload hash、错误分类和查询入口。

当前 Recommend adapter 还有一个应在后续代码阶段优先复核的边界：`start_recommendation()` 的 contract gate 检查了 recommend operation/request/idempotency header，但没有把通用的 confirmation、form revision、payload hash header 一并列为必需条件；`_recommendation_headers()` 在配置缺失时仍会把这些配置转成请求头名。真实启用 gate 前必须修正并用“缺少任一 header 时零请求”的测试覆盖，否则 typed request 虽完整，传输层可能没有真正发送确认和 payload 绑定信息。

错误应继续由确定性代码按配置错误、权限、业务拒绝、不可重试参数、可重试读取、写入 unknown、结果不完整和对账需要分类；模型只能读取有限摘要，不能根据错误文本决定重试或写入。以上 ACI 结论为：业务调用边界**已部分落地**；名单 ACI 和 Recommend header gate 属于**建议调整**；真实错误/幂等含义属于**外部阻塞**。

### 10.5 参数保真、候选 ID 和结果一致性

当前 ID 类型边界总体清楚：Agent workflow `task_id` 是 UUID；Java Campaign/Task 是严格正整数；`downstream_task_id` 是字符串；Java `kolBackendId` 和 Agent `selected_kol_ids` 是字符串。当前 `JavaRecommendationCandidate.kol_backend_id` 使用 `StrictStr`，没有数值转换、浮点转换或排序重写；这是已有代码和本地 schema 验证支持的正面结果。

但子阶段 4 尚未具备最终选择校验：

- 当前 `ConfirmSelection.selected_kol_ids` 要求至少一个字符串，但没有去重、candidate snapshot 归属、candidate page coverage、KOL source 或当前 result hash 校验；`SelectionDraft` 也没有这些规则。未知 KOL、重复 ID、已拒绝 ID、候选全集之外 ID、取消选择和空最终名单尚无确定性语义。
- Java DTO 明确 `kolBackendId` 是非空 String；每个条目 `selected=true` 表示选择，`selected=false/null` 表示取消，`rejected=true` 或非空 rejectReason 优先表示拒绝。Java `SelectionRequestDTO` 要求非空 items，item 要求非空 kols，但 DTO 本身没有 selection revision、candidate snapshot、operation ID 或 confirmation ID。不能把这些服务端不接收的字段写成已得到服务端并发保护。
- `batch-select` 是持久化的逐 KOL select/cancel/reject 事务并写历史，不能在 Agent 中称为“仅前端草稿”；但一次显式 selection save 事件与“最终封板确认”仍应保持不同权限和状态。`batch-confirm-selection` 在 Java `SelectionConfirmServiceImpl` 中先执行 selection，再对涉及任务 `finishTasks`，同一事务完成封板；Agent 必须把它作为高风险最终动作处理。
- 当前 `ConfirmSelection` 的事件名为 `confirm_selection`，而技术文档示例使用 `confirm_selection` 信封、首轮部分文字又使用 `selection.confirm`。事件命名需在前后端统一；不能让同一语义出现两个 discriminator。它当前缺少显式 campaign/business task 字段，虽然 Agent task UUID、run ID 和 actor runtime 可以间接绑定，最终 adapter 仍应显式生成并核对 Java 业务 ID。
- Java `batch-select` 支持取消和拒绝，但当前事件只有 `selected_kol_ids`，没有表达 `rejected`、`rejectReason` 或逐 KOL cancel 的完整 typed 语义。不能将 `selected_kol_ids=[]`、缺少字段和“取消所有已选”混为一谈。

因此，KOL 字符串 ID 的基础参数保真属于**已覆盖**，选择集合语义和 Java/Agent/前端三方 contract 属于**未实现且需调整**。子阶段 4 的确定性校验应拒绝重复/未知/快照外 ID，并返回当前候选与 selection 状态；不应由模型过滤、排序、补全或猜测用户意图。

### 10.6 候选全集、分页、snapshot 和版本一致性专项结论

当前不能把结果投影作为最终确认依据。原因不是投影代码本身失败，而是 Java 源码只证明 results 返回 `summary + kols[]`，没有证明分页、候选总数、不可变 result version、snapshot hash 或按 version 重读能力；本地代码主动将 `candidate_universe_complete` 设置为 `False`。最多 20 条 preview 也没有 cursor、omitted count 或 page number，不能支持“查看更多”或完整选择。

候选事实关系应固定为：

1. Java 原始 results：外部返回的完整性仍待契约确认，来源字段和服务端用户圈选状态是外部事实。
2. Agent snapshot：如果外部只能给一次完整 results，Agent 才能保存带 operation/request/downstream ID、抓取时间、内容 hash 的本地副本；如果结果是分页，必须记录每页 cursor、覆盖范围、版本和是否已取完。
3. Candidate projection：面向 UI/模型的有限预览，不能替代 snapshot，也不能作为最终 ID 白名单，除非完整性已被确定性代码证明。
4. User selection：独立 revision 和 selected/rejected/cancelled 状态，必须绑定某一 snapshot/result version。
5. Final confirmation：重新鉴权并重新读取当前 selection、Task 状态和候选版本；成功回执才表示封板。

在完整性契约未确认前，子阶段 4 本地可以实现不可提交的候选审阅、模型/用户选择草稿和明确的“结果不完整，暂不可确认”状态；不能构建声称全量的最终确认快照，也不能对真实环境执行 `batch-select` 或 `batch-confirm-selection`。`candidate_snapshot_ref` 可以作为 Agent 内部本地对象引用，但不能伪装成 Java snapshot/version；真实最终确认需要 Java 或 Agent durable store 提供可重读、不可变或可比较的来源。

推荐重跑、Task 更新、结果刷新或外部 selection 改变时，旧 run/snapshot/selection confirmation 必须失效。当前 `apply_form_patch` 会阻止部分推荐运行态的 Task 字段编辑，但没有子阶段 4 的 selection generation、外部 result version 或旧 snapshot 失效机制；`poll_sequence` 只防本地旧事件，不能证明服务端结果没有变化。该项是子阶段 4 contract gate 和真实联调验收项，不是本地 preview 投影的失败。

### 10.7 名单编辑、最终封板和危险操作

当前没有名单编辑或最终封板的 Agent 执行链：`ConfirmSelection` 只是已有事件模型，`interpret_event` 尚未接收它，`select_candidates`、`wait_for_selection_confirmation`、`reserve_selection_confirmation`、`submit_confirmed_selection` 仍显式未实现。因此当前不存在“推荐完成自动提交名单”的代码路径，这一安全边界是由未实现状态保持的，不能写成已经具备完整确认能力。

建议的本地阶段顺序是：

1. 先读取并建立候选 review projection，明确 AI `source/shortlist/backup` 与用户 selection 分离。
2. 在 Agent 内保存 `selection_patch`/selection draft，递增 selection revision，验证字符串 KOL ID 和当前 snapshot 覆盖；如果业务决定调用 Java `batch-select`，再单独进入持久化 selection operation。
3. 展示独立最终确认卡，生成新的 confirmation ID、operation/request ID 和 canonical payload hash；最终事件必须来自当前卡片的结构化交互，不能由“可以”“按推荐提交”、历史确认、模型总结或结果文本替代。
4. 预留 selection operation 后重新读 Task、当前候选/result version、selection revision 和 actor；只有正式契约允许且全部一致时调用 `batch-confirm-selection`；未知响应进入对账，不盲目重试。
5. 成功回执后才将 workflow 标记 completed/ended；受理、保存选择、封板成功、unknown 和部分失败分别展示。

需要特别区分“保存编辑”的确认语义：Java `batch-select` 已明确是正式持久化写操作，不是纯前端草稿；但产品是否要求每次点击保存都弹出与最终封板同等级的确认，当前资料没有完全签收。建议产品确认“用户点击保存并发送结构化 selection save event”是否本身就是对该可逆编辑写入的明确授权，并保持它与不可逆/高风险封板确认不同。任何情况下，模型推断不能代替保存事件，普通保存不能路由到最终封板。

Java `SelectionConfirmServiceImpl` 的事务可以避免“保存选择成功但封板失败”的服务内部分裂，但 Agent 尚未确认其重复提交回执、请求幂等、超时查询、已 ended 任务行为和回滚/重放语义。子阶段 4 本地 mock 可覆盖这些状态分支；真实 Java/UAT 写操作必须等待这些 contract gate。

### 10.8 unknown、重复事件、恢复、提示注入和可观测性

- Recommend 当前用 `operation_id/request_id/idempotency_key/confirmation_id` 关联；这些字段可复用于 selection，但没有 durable ledger。`start_attempted` 和本地 `write_outcome_unknown` 只能在当前进程状态中防一部分重复，不能覆盖“POST 已到 Java、进程在记录 start_attempted 前崩溃”的窗口。未来若恢复时看到 `CONFIRMED` 且没有持久化发送前/发送后提交点，不能安全地自动执行 `START_ONCE`；应先查询/人工对账。这个风险属于子阶段 5 的 durable operation 设计，但会影响子阶段 4 写操作复用同一原则。
- 重复同一 confirmation ID 且 payload 相同，应返回原回执；相同 ID 不同 payload 应拒绝；旧 selection revision/snapshot/actor 应拒绝并返回当前状态。这些约束目前没有数据库唯一键、事件 body hash 或 selection service，只有 Recommend 本地内存/对象状态测试。
- 轮询重复/乱序保护已有本地测试，`poll_sequence` 不会让旧事件覆盖新 run；但它不替代 Java result version。结果读取错误有有限连续失败计数，仍不等于 durable worker lease 或跨实例唯一轮询。
- Java 返回的名称、描述、推荐理由、source 和 reject reason 都是 data；KOL ID 是字符串标识，不能由文本生成。当前 projection 不执行 `eval`，但没有 LLM/context builder、消息角色隔离或提示注入测试。子阶段 4 应验证恶意推荐理由不能改变 selected IDs、confirmation 或 route。
- 当前没有日志实现或脱敏测试；代码没有证据显示 Bearer token、完整结果或完整错误已被安全审计。后续至少保留 operation/request/confirmation/task/revision/hash/status 和 source ref，默认不记录 token、内部 URL、完整 KOL 结果、完整理由或未经脱敏错误正文。

状态分类：轮询序列和本地 unknown 防重为**已在本地测试覆盖**；跨进程重复副作用、selection 幂等、恢复前重读和审计日志为**子阶段 5/真实契约后续实现**；提示注入边界为**设计已考虑，代码未接入**。

### 10.9 子阶段 4 准入评估

#### 子阶段 4 本地实现前必须明确

- `CandidateProjection` 的字段语义：preview 数量与全集总数分开，部分结果必须有明确不可提交状态；是否允许先做不可提交的 review draft。
- Java/Agent/前端事件命名，尤其 `selection_patch`、保存编辑事件和 `confirm_selection` 的 discriminator。
- 空选择、取消、拒绝、重复 KOL ID、未知 KOL ID、候选外 ID 和跨任务 selection 的业务语义。
- `batch-select` 是由用户显式保存事件触发的持久化编辑，还是先只保存 Agent 草稿；两者不能混为“无副作用编辑”。
- AI 推荐、backup、用户 selected/rejected 和最终 submitted 集合的独立存储字段。

#### 子阶段 4 contract gate 必须完成

- 结果是否完整、如何分页、total/cursor/版本如何读取，是否有不可变 snapshot 或可比较 hash。
- Java `batch-select` 和 `batch-confirm-selection` 的正式请求路径、响应/回执、任务状态要求、对象权限、重复请求、超时查询和 unknown 对账。
- selection revision、candidate snapshot/version、confirmation ID、operation/request/idempotency 是否由 Java 接收并校验；若不接收，Agent 只能做本地前置校验，不能声称服务端提供强一致保护。
- 已 ended 任务是否仍可 batch-select，confirm 重复提交返回什么，事务失败如何恢复。

#### 可以随子阶段 4 本地实现完成

- 仅针对 mock/full-fixture 的候选分页阅读、模型拟选 ID 子集校验、用户 selection draft、revision 递增、取消/拒绝字段和不可提交提示。
- 确定性确认节点的 actor/task/run/snapshot/revision/hash 检查、重复/过时确认拒绝、accepted/unknown/failed UI 状态。
- 使用不包含真实写授权的 mock adapter contract tests，验证普通保存和最终封板调用永不混用。

#### 只阻塞真实 Java/UAT 写操作

- Java 真实部署路径、Venus OAuth 委托身份、brand/tenant 传递、Java 是否支持 operation/request/idempotency/confirmation/revision/hash headers。
- batch-select/batch-confirm-selection 的真实错误 envelope、重复回执、对象权限、已封板状态和超时对账。
- Java results 在真实环境的完整性、分页、候选数量和稳定版本。

#### 留到子阶段 5

- PostgreSQL selection/operation ledger、唯一约束、事件 body hash、Outbox、worker、lease/fencing、checkpoint 恢复和跨进程 snapshot。
- 进程崩溃窗口、SSE 重连、同一事件重复消费、旧权限撤销后恢复和 durable 原始结果引用。

#### 第一阶段整体联调前验证

- 端到端从真实 Recommend 完成到全量候选卡、selection 编辑、最终封板和刷新恢复。
- 100–200 条运营表达的模型抽取/澄清评测，以及长结果、恶意文本、权限和重复确认回归。

#### 当前不适用或只需保留扩展

- Shell、文件系统、浏览器、通用 MCP/插件和任意数据库工具：当前项目没有这类能力，不增加。
- KV Cache、复杂向量检索、复杂摘要优化：保留稳定分区和接口位置，不阻塞子阶段 4 正确性。

综合判断：**可以在本轮审核通过后进行子阶段 4 的本地、不可对外写入实现**，但应先把候选 review/selection draft 与最终封板 adapter 分开，且在全集/分页契约未确认时强制不可提交。若子阶段 4 的目标包括真实 `batch-select` 或 `batch-confirm-selection`，则当前不能进入真实写入阶段；真实写请求应继续保持关闭。

### 10.10 候选全集与最终确认专项结论

- 当前结果投影不能作为最终确认依据。它可以作为有限 review preview，不能授权用户选择全集外的 ID，也不能让 UI 显示“全部候选”。
- 候选全集和分页未确认会阻塞：完整候选卡、查看更多、准确总数、候选全集 ID 校验、候选外 ID 拒绝的强证明，以及以当前全集为依据的最终封板。它不阻塞先做 mock 的不可提交审阅卡和 selection draft。
- `candidate_snapshot_ref` 当前有 Agent 生成的本地 UUID 和 hash，但缺少真实来源的 version/cursor/coverage/immutability。需要 Java 或 durable Agent snapshot contract 提供结果版本、完整性、重新读取和过时判断，不能把本地 UUID 写入 Java 当作服务端版本。
- `selection_revision` 当前只有本地 `SelectionDraft.revision`/事件字段，缺少服务端当前版本读取和 compare-and-set；Java DTO 没有该字段。需要前端、Agent 和 Java owner 共同确定版本的权威方。
- 普通名单保存与最终封板仍有一个产品确认点：Java 事实已表明 `batch-select` 会持久化选择/取消/拒绝，`batch-confirm-selection` 会在事务内保存选择并封板；需要确认 Agent 的保存按钮是否直接触发前者、是否要求独立轻量确认，以及最终确认是否只允许后者。两者必须保持不同 operation 和不同 UI 状态。
- 当前不能对真实环境执行 `batch-select` 或 `batch-confirm-selection`。在本地可以实现 mock contract 和不可提交审阅；真实调用须等待完整性、身份、幂等、revision/snapshot、回执和 unknown contract。

### 10.11 对现有方案和 TODO 的影响

本轮不建议返工 base 或子阶段 1–3 的基本方向。typed adapter、确认隔离、Task 状态映射和 unknown 保守策略与首轮决策一致；需要的是在进入名单流程前修正文档/契约边界和若干实现约束，不是把本地子阶段 2、3 改成整体完成。

建议下一次调整 TODO 时：

1. 在子阶段 4 前增加“候选全集/分页/结果版本 contract gate”，将“本地 review draft 可做”和“最终确认写入不可做”分开记录。
2. 增加 selection draft、selection patch、batch-select、batch-confirm-selection 四者的独立语义和 typed contract 验收项。
3. 增加 KOL 字符串 ID 的重复/未知/候选外/取消/拒绝/空选择测试，以及旧 snapshot/revision/confirmation 失效测试。
4. 增加最终确认 timeout/unknown、相同 confirmation 重放、同 ID 不同 payload、刷新恢复和服务端回执查询验收项。
5. 明确 Recommend header gate、snapshot source/storage owner、AgentState 重复 run 字段和 durable start reservation 的实现整改项。
6. 子阶段 4 完成标准应写成：本地不可提交 review/selection 流程可验证；真实封板只有在 Java contract/UAT 完成后才可标记真实验收通过。

上述是 TODO 修改建议，本轮没有直接修改 TODO。operation ledger、跨进程恢复、事件去重、Outbox、worker、SSE 和完整鉴权继续明确留给子阶段 5；KV Cache、通用工具和复杂向量召回继续作为扩展或不适用项，不应阻塞本地 selection 领域建模。

### 10.12 待确认问题

**需要产品/架构确认：**

- 当前较新决策是否正式覆盖技术文档中“Campaign 不增加 Agent 专属确认”的旧表述，统一为 Campaign/Task/Recommend 各自确认，还是只让 Task/Recommend 保持独立确认。
- Agent 点击“保存编辑”是否直接授权一次 `batch-select` 持久化写入；该保存是否需要轻量确认，和最终封板确认的 UI/状态差异是什么。
- 空最终名单、全部取消、拒绝名单、部分选择和跨任务 selection 是否允许；Agent task 是否严格限制为单一 business task。
- `batch-confirm-selection` 是否是 Agent 唯一最终封板入口，已封板后是否完全禁止编辑。

**需要 Java/API owner 确认：**

- Recommend、Task read/list、results 的正式部署 base path、Venus OAuth 委托身份、tenant/user/brand 传递和自定义 header 支持。
- Recommend、batch-select、batch-confirm-selection 是否接收 operation/request/idempotency/confirmation/revision/payload hash，是否提供按 request ID 查询和重复回执。
- results 是否完整、是否分页、total/cursor/version/hash 如何定义，`kols[]` 是否稳定排序，是否能按版本重新读取。
- selection revision、candidate snapshot/version 和候选 ID 校验由哪一方负责；当前 Java Selection DTO 没有这些字段。
- batch-select/batch-confirm-selection 的错误 envelope、权限、空/重复/未知 KOL、取消/拒绝、已 ended 状态、超时和事务失败行为。

**需要前端确认：**

- 当前名单卡的 save、cancel、reject、final confirm 事件和 discriminator；`confirm_selection` 与 `selection.confirm` 选哪一种。
- UI 是否展示分页/总数/不完整结果警告、当前 selection revision、snapshot/result version、unknown/对账状态和刷新恢复提示。
- 用户看到的 KOL ID、排序和推荐/备选/已选标记如何映射到 Java `kolBackendId`，以及空选择如何提交。

**需要模型/平台负责人确认：**

- 标准 tool result/context schema、source/data 标记、候选 preview 的字段白名单和结果引用读取方式。
- 真实模型结构化输出、有限重试、摘要覆盖边界和模型不可用时的人工 selection 路径。KV Cache 能力只需在模型接入和成本评估阶段确认。

**需要测试服务/UAT 验证：**

- Recommend accepted → Java polling → completed → results 的真实链路，包含失败、结果读取失败和响应丢失。
- 结果初始数量与“查看更多”的全集/分页关系、重复/刷新结果和候选版本变化。
- batch-select 与 batch-confirm-selection 的真实写入、重复、超时、已封板和权限行为。当前不能用本地 40 passed 替代这些证据。

### 10.13 建议后续验证清单

| 验证项 | 建议阶段 | 目标 |
|---|---|---|
| Candidate preview 与 candidate universe 分离 | 子阶段 4 本地 | 投影永不被当成全集；不完整时禁用最终确认 |
| results 分页、总数、cursor、版本/hash | 子阶段 4 contract/UAT | 证明候选覆盖范围和刷新一致性 |
| KOL ID 严格字符串、重复/未知/候选外 ID | 子阶段 4 本地 + UAT | 防精度损失、误选和跨结果提交 |
| AI recommend/backup/model selected/user selected 分离 | 子阶段 4 本地 | 保留各自事实和 UI 来源 |
| save selection 与 final confirm 两个不同事件/operation | 子阶段 4 本地 | 防普通保存触发封板 |
| 未确认不得封板 | 子阶段 4 本地 | 模型总结、聊天文本和旧确认均不能授权 |
| 旧 revision/snapshot/confirmation 失效 | 子阶段 4 本地 + 子阶段 5 | 防旧名单覆盖当前选择 |
| 相同 confirmation 重放与同 ID 不同 payload | 子阶段 5 | 返回原回执或拒绝冲突 |
| batch-select 的取消/拒绝/空集合/跨任务语义 | 子阶段 4 contract/UAT | 对齐 Java DTO 和产品行为 |
| batch-confirm-selection 事务、已 ended、重复和回执 | 子阶段 4 UAT | 确认封板真实副作用和不可逆边界 |
| 最终确认 timeout/unknown 与 request ID 对账 | 子阶段 4–5/UAT | 禁止盲目重复封板 |
| 恶意推荐理由/名称/错误文本不改变名单和确认 | 子阶段 4 本地 + 整体联调 | 保持不可信数据边界 |
| 恢复后重新读取 Task、snapshot、selection 和 operation | 子阶段 5 | 禁止旧 checkpoint 自动执行写操作 |
| 大型结果有界投影和原始引用 | 子阶段 3/4/LLM 接入前 | 防模型上下文溢出和敏感数据扩散 |
| Recommend header gate 缺失配置零请求 | 子阶段 3 后续本地修正 | 防确认/hash 只存在于 Python 对象而未进入 HTTP |
| 真实 Java/UAT 全流程 | 整体联调 | 证明真实身份、幂等、结果和封板能力 |

第二轮结论：首轮关于确定性 typed adapter、独立 Recommend 确认、受理/完成分离、unknown 不自动重试、外部文本不可信和不引入通用工具的方向基本得到本地代码支持；名单编辑、候选全集、分页、selection revision/snapshot 服务端来源、最终封板和 durable 恢复仍是下一阶段的主要边界。当前建议在审核本章结论后进入子阶段 4 的**本地、不可对外写入部分**，同时把候选完整性和最终确认 contract gate 置于真实封板之前；本轮不改变 TODO、不修改代码，也不执行任何真实业务写请求。

### 10.14 用户确认后的决策分析与多 Workflow 会话模型

本节记录本轮用户对第 10.12 节问题的选择和补充说明。以下内容是本轮形成的产品/架构决策，应在后续调整 TODO、代码和接口契约时作为依据；其中涉及 Java 服务端是否支持某字段或回执的事项仍然需要外部验证。

#### 已确认的选择

| 事项 | 本轮选择 | 对实现的含义 |
|---|---|---|
| Campaign/Task/Recommend 确认 | 三类业务写操作都保留独立确认 | 统一采用 Campaign create、Task create、Recommend start 各自独立 preview/confirmation；技术文档中“Campaign 不增加 Agent 专属确认”的旧表述需要后续按本轮决策修订，但本轮不直接改设计文档 |
| 子阶段 4 范围 | 本地不可写 | 可以做候选审阅、selection draft、确定性校验、mock contract 和不可提交状态；不发送真实 `batch-select` 或 `batch-confirm-selection` |
| 结果不完整时 | 只读审阅并禁用最终确认 | 未确认全集、分页或稳定版本前，允许展示有限 preview 和本地草稿，但不能显示为完整候选，也不能封板 |
| 普通保存编辑 | 先保存 Agent 本地草稿 | 用户编辑先更新本地 selection draft；Java `batch-select` 的正式持久化写入要等契约确认后单独接入，不能把本地编辑误报为 Java 已保存 |
| 最终封板入口 | 只用 `batch-confirm-selection` 语义 | 不把 `batch-select` 或 `batch-finish` 当作 Agent 最终提交入口；真实入口、回执、幂等和 unknown 仍需 Java/UAT 验证 |
| 最终确认事件名 | `confirm_selection` | 前后端统一使用该 discriminator；不同时接受 `selection.confirm`，避免两个语义相同的事件进入系统 |
| 空最终名单 | 默认拒绝 | `selected_kol_ids` 为空时不能最终封板；全部取消可以作为本地编辑状态，不能直接变成空名单完成 |
| 缺少服务端 revision/snapshot 校验 | Gate 关闭 | Agent 可以做本地前置校验，但 Java 不接收或不校验 selection revision/candidate snapshot 时，不发送真实封板写请求 |

#### 会话与多个 Workflow 的确认模型

一次会话可以包含多个独立 workflow。通常会话有一个“当前正在交互的 workflow”，用户可以在多个 workflow 之间切换；这与“同一时间只能存在或运行一个 workflow”不同。

每个 workflow 独立绑定以下事实：

- Agent workflow/task ID；
- Campaign 和 Java business task；
- 当前 form revision；
- Recommendation run、operation/request/confirmation；
- downstreamTaskId、结果引用和候选 snapshot；
- selection draft、selection revision 和最终确认状态；
- 当前 pending UI、unknown/对账状态和下一步动作。

会话层负责保存 workflow 列表、当前 workflow 指针、用户可见的标题/状态摘要和切换记录；它不把多个 Campaign、Task 或 selection 合并成一个业务事实。后续持久化时，conversation 与 workflow 应是一对多关系，workflow 是业务操作和恢复的隔离边界。

一条用户消息在首版通常只路由到一个明确的 workflow：

```text
当前 workflow A
  └─ “预算改成 20 万” → 只交给 A

当前 workflow A
  └─ “新建活动 2” → 创建 workflow B，切换当前交互对象到 B

当前 workflow B
  └─ “继续任务 A” → 解析并切回 A，后续消息交给 A
```

这意味着后续意图识别节点的职责应是“确定目标 workflow + 受限意图/字段提议”，而不是同时驱动多个 workflow。结构化事件中携带的 workflow/task 绑定优先于模型推断；自然语言目标不明确、同名 Campaign/Task 有歧义或目标不唯一时，必须先展示候选并询问，不得猜测或广播给多个 workflow。

#### 新建、切换和后台执行规则

本轮确认的切换行为如下：

1. 用户明确提出“新建活动”或“新建任务”时，系统创建对应的新 workflow，并切换当前交互对象到新 workflow。
2. 切换前不要求完成或取消旧 workflow。旧 workflow 的草稿、pending confirmation、执行中 Recommend、结果读取和 unknown 状态继续保留。
3. 切换到新 workflow 时，UI 必须提示旧 workflow 尚未完成；提示是状态告知，不是取消旧任务，也不等于旧任务已经对账完成。
4. 旧 workflow 的后台执行或对账可以继续，但不能因为新 workflow 被创建而自动取消、重启或重新提交旧 operation。
5. 用户明确说“继续任务 A”或明确指定已有 Campaign/Task 时，系统先校验目标的唯一性、当前 actor、tenant、brand 和对象权限，再切换当前 workflow。
6. 用户只提供普通补充信息时，默认应用于当前 workflow；不会因为消息中出现与其他 workflow 相似的名称就自动改绑。
7. 目标不明确、同名对象有歧义或“新建”意图不足以确定 Campaign/Task 边界时，系统暂停并请求选择；不创建对象、不修改旧 workflow、不发送业务写请求。

“切换当前 workflow”本身是 Agent 会话状态变化，不是 Campaign/Task/Recommend 业务写入，因此不需要 Java confirmation；但切换后的任何 Campaign、Task、Recommend、selection save 或 final confirm 写入，仍必须重新绑定当前 workflow 的 actor、目标对象、revision、operation/request 和 confirmation。旧 workflow 的 confirmation 不能授权新 workflow，当前 workflow 的自然语言也不能覆盖旧 workflow 的 pending confirmation。

#### 对意图识别节点的影响

本轮多 workflow 决策使意图识别节点的输出至少需要分成两层：

```text
IntentProposal
├─ target_workflow: current | existing_workflow_ref | create_new_workflow
├─ intent: update_form | start_recommendation | review_candidates |
│          edit_selection | confirm_selection | switch_context | other
├─ structured_patch / target_reference / ambiguities
└─ evidence
```

该节点不能直接改变当前 workflow、创建 Campaign、启动 Recommend、保存 selection 或封板。确定性节点应先校验目标 workflow 和权限，再根据意图生成结构化事件或 patch。尤其是：

- “预算改成 20 万”只能形成当前 workflow A 的字段 patch 提议，并检查 A 的 form revision；
- “新建活动 2”只能形成创建新 workflow 的提议，随后进入 Campaign 字段收集和独立 Campaign confirmation；
- “继续任务 A”只能形成 workflow 切换提议，必须先解决对象唯一性和权限；
- “按推荐提交”只能形成需要用户操作确认的意图线索，不能直接产生 `confirm_selection`；
- `confirm_selection`、selection save 和其他结构化 UI 事件绕过 LLM 意图识别，直接进入确定性校验。

因此，意图识别节点适合放在可信上下文准备和结构化事件分流之后，仅处理自然语言 `UserMessage`；它不能位于权限上下文之前，也不能成为通用业务工具路由器。该结论支持未来自然语言接入，但不要求本轮或子阶段 4 立即实现模型节点。

#### 对子阶段 4 和子阶段 5 的影响

子阶段 4 本地实现应以一个 workflow 为 selection 操作边界，但不应把整个会话限制为一个 workflow。候选 review、selection draft、selection revision、confirm preview 和不可提交状态均写入当前 workflow；会话 UI 只负责在 workflow 之间切换和展示状态摘要。

子阶段 4 本地必须覆盖：

- 当前 workflow 切换不会改变其他 workflow 的 draft、run、snapshot 或 pending operation；
- 普通补充信息默认修改当前 workflow；明确新建会创建新 workflow；明确旧对象会切回既有 workflow；
- 同名或权限不明确时不猜测目标；
- 旧 workflow 的 Recommend unknown/in-progress 不因新 workflow 自动取消或重启；
- 当前 workflow 的旧 confirmation、旧 snapshot 或旧 selection revision 不能在切换后继续授权写入；
- 本地 selection draft 可以为空或全部取消，但最终 `confirm_selection` 默认拒绝空名单。

子阶段 5 必须持久化并串行化会话/ workflow 关系：`conversation_id + current_workflow_id` 是 UI 交互指针，workflow 自身的 operation/selection ledger 是业务事实；恢复时先校验 workflow 所属会话和 actor/brand，再加载对应 checkpoint。多个 workflow 可以后台运行，但同一 workflow 的事件、副作用和 checkpoint 仍需独占 lease/fencing，不能因为会话存在多个 workflow 就允许同一 workflow 并行消费。

当前产品例子支持“一个 workflow 关联一个当前 Campaign/Task 工作目标，多个目标作为同一会话下的多个 workflow”。但本轮没有明确确认单个 Campaign workflow 是否可同时拥有多个 Java recommendation Task。子阶段 4 建模仍按一个 selection/run 绑定一个明确 business Task；若未来一个 workflow 要管理同一 Campaign 下多个 Task，应先扩展 workflow→task 的关系及独立 selection ledger，而不能把多个 taskId 塞进一个确认事件。

#### 本轮仍未解决的外部问题

本轮已经决定产品侧的 Agent 行为，但没有替 Java/API owner 回答以下事实问题：

- Java 是否接收和校验 confirmation、operation/request/idempotency、selection revision、candidate snapshot 或 payload hash；
- results 是否完整、是否分页、是否存在稳定版本；
- batch-select 与 batch-confirm-selection 的真实重复回执、unknown 查询、已封板任务行为和错误 envelope；
- 真实服务能否按 Campaign/Task/brand 对多个 workflow 做对象级授权；
- 当前 Agent session/workflow API 尚未实现，不能把本轮的多 workflow 决策写成已有运行能力。

这些问题继续作为真实联调和后续 durable 实现的 gate。本轮只确认了目标产品语义和本地实现边界，没有发送任何真实写请求，也没有修改业务代码或 TODO。

### 10.15 已选方案的权衡分析

下表解释本轮选择相对其他候选方案的收益、代价和影响。此处记录的是选择依据，不表示相关 Java 服务端能力已经存在。

| 问题 | 已选方案 | 其他方案及未选原因 | 收益、成本与影响 |
|---|---|---|---|
| Campaign 是否独立确认 | Campaign、Task、Recommend 都有独立确认 | 不给 Campaign 增加 Agent gate 可更贴近技术文档旧表述；但会与本轮选择及当前代码不一致 | 高风险副作用前用户看到操作，语义统一；代价是 Campaign 创建多一个 UI/状态步骤，并须后续修订旧文档与测试。不是本地 code-only 争论，属于本轮确认后的新产品决策 |
| 子阶段 4 先做到哪里 | 本地不可写 | 真实写入需外部权限/幂等/回执可靠；完全等待契约会阻断领域模型和 UI 状态开发；先接 mock 写入容易被误认为 Java 已就绪 | 可验证 selection 语义和安全失败状态，不触碰业务；代价是本地流程不能宣称 Java 已保存或封板，真实写 adapter 保持禁用 |
| 候选不完整时 | 可只读审阅/草稿编辑，但禁最终确认 | 仅警告仍可能被用户/模型当成完整集；完全不展示会失去 review 和页面流程验证能力 | 保留进展同时守住全集边界；需要 UI 显示不完整状态、disable confirm，并让服务端写 gate 也拒绝不完整快照 |
| 普通保存编辑 | 先写本地 Agent draft，未来 Java batch-select 另行接入 | 立刻调用 batch-select 会将用户编辑持久写入 Java，而其幂等、版本和真实操作确认仍未确认；额外“轻确认”也可能把每次编辑变成繁琐双击 | 当前可开发 selection 状态而不制造外部副作用；接入 batch-select 时必须再确认结构化保存事件是否构成明确授权，并记录独立 operation |
| 最终封板 API | 只用 batch-confirm-selection | `batch-select + batch-finish` 是两个外部操作，存在中间状态/部分失败；`batch-finish` 单独封板不会携带本次选择 | 与 Java `SelectionConfirmServiceImpl` 的事务“先 select 再 finish”一致，避免拆分不原子操作；回执、重复请求和 unknown 仍须 Java/UAT 验证 |
| 一条消息的目标与会话结构 | 一个会话含多个 workflow；通常一个当前交互 workflow；每条消息路由到一个明确 workflow | 单 workflow 切换/改绑会丢失或覆盖旧状态；一个消息并行路由多个 workflow 会让同一自然语言产生多个副作用目标 | 保留任务 A 状态并可切回；“新建活动 2”生成新 workflow，“预算改成 20 万”只作用当前 workflow；代价是需实现当前 workflow 指针、workflow 列表和目标歧义澄清。持久化隔离留子阶段 5 |
| 最终事件名 | `confirm_selection` | `selection.confirm` 也能表达动作，但会与已有类型/协议出现双重 discriminator | 沿用现有 `ConfirmSelection` schema 和技术文档信封名称，降低前后端适配歧义；若未来改名应做有版本的迁移，不要静默双接受 |
| 空名单 | 默认拒绝封板 | 允许空名单需要明确业务上“选人完成但零人”的有效性、后续状态和回执；自动把空数组解释为取消会产生隐式批量操作 | 保守拒绝可防止空/缺失字段意外完成任务；本地测试要区分 empty、omitted、cancel-all；若业务要支持零候选，应通过明确业务决策增加新语义 |
| Java 缺少版本校验 | 真实写 gate 关闭 | Agent 本地 hash/revision 可以降低误操作，但无法阻止两次读取之间 Java/其他用户修改造成的竞态 | 避免虚称强一致；代价是即使本地预检通过，也不能在 Java 无 CAS/snapshot 校验时确认真实封板。可先与 Java 协商增加版本字段或服务端读取-校验-写入事务 |

选择后仍未关闭的事项包括：Java 是否支持幂等及 request ID 查询；results 的完整性/分页/版本字段；selection revision 和 snapshot 的权威来源；batch-confirm 的真实回执和已封板行为；单个 Campaign workflow 是否允许承载多个 recommendation Task；当前确认决策如何在第一阶段技术文档中统一表述。以上必须依责任方签收或真实联调，不由本表的产品选择推定为外部事实。

### 10.16 三项新增需求的设计变更说明

本轮未修改业务代码、测试、依赖或代码索引，只将三项新增需求写入技术实现文档、实施计划和内部 TODO；子阶段 2、3 的真实状态没有改变。

- **输入分流与意图提取：**未来复用当前 `interpret_event` 最小调整为 `dispatch_input`，置于 `prepare_context` 之后；结构化事件绕过模型，`UserMessage` 进入 typed proposal → `validate_proposal` → patch/query/clarify/unsupported/recoverable error。意图提议不能指定身份、业务 ID、operation、API 或 graph 节点，也不能生成确认凭据。当前只完成设计，实施时机是 LLM 自然语言能力接入，不阻塞子阶段 4 结构化事件流程。
- **Vue3 表单回显：**推荐服务端校验后的 form snapshot/patch 通过 API/SSE 进入 Vue3 `ref`/`reactive`/Pinia；服务端 form revision 权威，旧事件和冲突 patch 不覆盖用户编辑，回显不代表业务提交。Playwright 只保留为 E2E 或不可修改外部页面的适配工具。协议需在子阶段 4 前定稿，真实前端集成与会话体验属于后续前端里程碑，持久化事件和 SSE 补拉依赖子阶段 5。
- **语音转文字：**只设计 `getUserMedia`/`MediaRecorder` → 批量 ASR → 输入框草稿 → 用户点击发送 → 普通 UserMessage 流程；原始音频默认不入 AgentState/消息/日志。云端 ASR、自托管 faster-whisper/Whisper 和独立服务需要质量、隐私、容量和成本 PoC；语音当前不实现，也不作为子阶段 2–4 或当前第一阶段验收前置。

上述新增设计继承本章已确认的多 workflow 规则：会话可以包含多个 workflow，通常只有一个当前交互 workflow，每条消息首版只路由到一个明确 workflow；“新建活动”创建新 workflow，“继续任务 A”切回旧 workflow，旧任务不因切换自动取消或重启。详细协议、实施顺序和验收边界见《第一阶段嵌入式Agent技术实现文档》第 14–17 节、《第一阶段嵌入式Agent实施计划》第 2–3 节及本 TODO 的子阶段 4 新增设计项。

### 10.17 子阶段 4 本地实现复盘（2026-11-19）

本轮在保留本章决策的基础上完成了子阶段 4 的本地不可写实现、专项测试、TODO 和代码索引同步；没有发送真实 selection 写请求，子阶段 2、3 的真实状态没有改变。

- 候选投影现在把 `fetched_count`、`preview_count`、`omitted_count`、未知 `total_count`、`truncated`、pagination verified 和 Java snapshot version 分开表达。当前 Java `summary + kols[]` 源码不足以证明全集，所以任何结果都仍标记 `candidate_universe_complete=false`；本地 snapshot ref/hash 仅用于当前进程校验，不能冒充 Java 版本。
- 结构化 selection 路由已经区分 `selection_review`、`selection_patch`、`selection_save` 和 `confirm_selection`。本地 draft 保留字符串 KOL ID，以及 selected/cancelled/rejected/rejectReason；用户操作不读取或改写推荐文本，恶意推荐理由不能改变名单。候选外 ID、重复 ID、空名单、全部取消、旧 snapshot、旧 selection revision、错误 workflow/Campaign/Task、actor/brand 和 form revision 均阻断。
- 普通保存和最终封板各有独立 preview、operation/request/confirmation、payload hash、状态和 typed request。保存使用 Java `batch-select` 形状，最终使用 `batch-confirm-selection` 形状，但当前 `SmartProposalAdapter` 只返回明确的 blocked receipt，不发 HTTP。因为 Java DTO 没有 snapshot/revision/幂等/对账字段，完整 fixture 也不会打开真实写 gate；最终成功只能来自未来相关且经验证的外部回执。
- 本地 graph 已防止已生成 snapshot 后迟到 poll 替换 projection 或 selection；unknown/late receipt 不自动重试，重启仍为 review/reconcile-only。selection receipt reducer 只接受当前 actor、任务/run/snapshot/revision/payload 完全关联的 fixture 回执；跨进程 ledger、唯一约束、Outbox 和恢复前重读仍属于子阶段 5。
- `SmartProposalAdapter.start_recommendation` 现在要求 confirmation ID、form revision、payload hash header 与其 contract flags 一起配置；缺任一配置在 HTTP 前返回 configuration rejected。专项回归覆盖三个缺配置分支和现有 Recommend headers。
- 验证结果为 `.venv/bin/python -m pytest -q`：**66 passed**，并通过 `.venv/bin/python -m compileall -q app tests` 和 `git diff --check`。本结果只代表本地 Pydantic/LangGraph/MockTransport 测试，不代表 Java/UAT 或子阶段 2–3 整体验收。按范围暂停在子阶段 4，不进入子阶段 5。

### 10.18 子阶段 5 本轮实现状态（2026-11-20）

首轮第 9 节和第二轮的持久化、恢复、去重、SSE 与身份要求已开始落地：新增 PostgreSQL schema、消息只增不改、事件唯一约束、表单事务、Outbox 和 fencing、受限 UI 事件回放以及默认拒绝的身份/品牌授权接口。独立 PostgreSQL 测试覆盖并发去重、租约、权限测试替身、快照和官方 LangGraph saver 的 interrupt/resume。

尚未闭合：`WorkflowService` 与 checkpoint、业务表和事件台账的恢复协调；过期 graph 执行器的 checkpoint fencing；非表单结构化事件的 durable 消费；真实认证及恢复重授权；SSE 过期游标与完整 snapshot；发送后未记录崩溃窗口的外部对账。当前独立 checkpoint 测试不等于端到端 durable 恢复，表单 worker 不是外部业务写 worker。上下文摘要仅有数据库基础，Context Builder、模型、意图识别、语音和 Vue 页面未实现。子阶段 5 未完成，子阶段 2–4 外部阻塞不变；没有真实 Java 写请求。
