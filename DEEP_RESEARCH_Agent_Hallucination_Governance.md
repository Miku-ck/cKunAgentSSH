# Deep Research: 降低智能体幻觉与工具结果失真的治理框架

> 生成日期：2026-11-24 | 深度：standard | 来源：15

## TL;DR

降低智能体幻觉不能只靠“提醒模型不要编造”。真正有效的做法是把智能体当作一个需要工程控制的执行系统：工具返回先成为带来源、时间、作用域和完整性的“观察”，再经过确定性校验、独立核验和时效裁决，最后才允许变成回答中的事实声明。结构化输出、模型自我批评和重复调用都有价值，但都不能单独证明事实正确；高风险动作还必须受到权限、审批和审计边界约束。

## Executive Summary

用户关心的“工具返回结果与事实不符”实际上包含三个不同问题：工具或数据源给错了结果；工具结果正确但模型读错了；结果曾经正确但已经过期。三者需要不同防线，不能用同一个“重试”或“让模型再检查一遍”解决。

现有研究表明，多智能体失败不能简单归因于底层模型，还包括系统设计、协作和任务验证问题；工具智能体评测也应检查最终状态和多次运行可靠性，而不只比较文本相似度。[1][2] 严格函数调用可以约束参数形状，却不保证参数语义正确，更不保证工具结果真实。[3] 对推理任务而言，没有外部反馈的自我纠正可能失败，甚至降低表现，因此关键事实应优先使用独立反馈或独立工具核验。[7]

本报告建议采用八层防线：任务契约、调用前校验、工具适配器、观察规范化、独立核验、时效与状态裁决、回答编译、策略门禁。核心原则是：**没有有效证据的事实声明不得输出；证据不足时明确限定或拒答；有副作用的操作不得仅凭模型判断执行。** 评估时必须分别衡量工具选择、参数正确性、原始结果有效性、证据覆盖、陈旧率、冲突识别、拒答校准和最终状态成功，避免用最终答案正确掩盖过程中的误读或危险动作。[2][11][13]

## 1. Status Quo [Confidence: Medium]

### 1.1 幻觉是系统级故障，而不只是模型级故障

多智能体系统的实证研究从 1,600 多条标注轨迹中归纳出 14 类失败模式，覆盖系统设计、智能体间错位和任务验证；这说明仅更换模型或调整提示词，无法解决大部分端到端可靠性问题。[1] τ-bench 则把工具智能体放入真实领域 API、政策约束和模拟用户环境中，用最终数据库状态与目标状态比较来评价任务，并通过多次运行测量可靠性。[2]

对单智能体工具调用同样可以借用这个视角：错误可能发生在选择工具、生成参数、工具实现、序列化、上游数据源、传输截断、结果解释或状态更新任一环节。治理对象因此应是完整证据链，而不是最后一段自然语言。

### 1.2 工具结果是观察，不自动是事实

一个负责任的系统需要区分两层陈述：

- **观察层**：某工具在某时刻、用某组参数、从某来源返回了某结果。
- **事实层**：该结果在所需时间、实体和版本范围内真实描述了世界。

这两层之间可能隔着缓存、最终一致性、采样误差、权限过滤、工具缺陷或上游错误。建议为每次调用保存工具及版本、精确参数、来源、观察时间、作用域、状态、完整性和转换记录。Redis 官方文档建议缓存设置 TTL 并在更新时失效对应键，以限制陈旧数据窗口；DynamoDB 文档则明确说明，最终一致读可能不反映刚完成的写入。[9][10] 这些例子说明，“调用成功”与“当前事实已确认”不是同一件事。

### 1.3 常见控制各自能证明什么

严格结构化函数调用在支持的模型和配置下，可以确保生成参数符合给定 schema；它证明的是接口符合性，不是参数语义适当性，也不是结果真实性。[3] 引用生成研究则显示，即使较好模型也会经常缺少完整引用支持，因此“有引用”不能替代逐主张核验。[6]

自我批评也不是可靠的外部真值。对推理任务的研究发现，模型在没有外部反馈时难以纠正自身错误，有时还会降低表现；这一结论不应被无限外推为所有事实核验任务的效应量，但足以支持“关键核验尽量脱离原生成路径”的工程选择。[7]

### 1.4 当前实践正在从答案评测转向轨迹与状态评测

Google 的评估指南把能力评估和质量评估分开，并覆盖工具选择、参数、顺序、groundedness 和人工评审。[11] τ-bench 的最终状态比较进一步说明，工具智能体不仅要“说得像”，还要“把事做对”。[2] 对防幻觉系统而言，这意味着不能只统计最终答案准确率，还要检查工具调用是否合法、原始结果是否被误读、拒答是否恰当以及危险动作是否被阻断。

## 2. Emerging Trends [Confidence: Medium]

### 2.1 从事后纠错转向证据链与可恢复性

2025—2026 年的工程讨论越来越强调“安静失败”：系统可能给出格式正确、语气自信但内容错误的答案。IEEE Computer Society 的一篇工程文章把语义校验、业务规则、grounding、校准不确定性、有限重试、幂等和语义熔断纳入容错设计，并明确提醒“通过验证不保证答案正确”。[5] 这不是正式标准，但反映了从单点模型防护转向分层系统防护的方向。

### 2.2 从静态问答转向动态状态维护

STALE 预印本研究隐含记忆冲突：后续观察没有明确否定旧记忆，却已经让它失效。其 400 个专家校验场景、1,200 条查询显示，检索到更新证据并不等于能抵抗旧前提并据此行动；最佳受测模型总体准确率为 55.2%。[8] 该数字只适用于该预印本基准，不能当作所有智能体准确率，但它揭示了一个重要趋势：时效治理必须进入状态更新和行动决策，而不能只做回答前的“再检索一次”。

### 2.3 从权限清单转向工具组合风险

OWASP 将提示注入、敏感信息泄露、供应链、输出处理不当、过度代理、错误信息和无界消耗列为不同风险。[14] 对智能体而言，单独看似无害的读取、搜索和发送工具，组合后可能形成危险能力链。因此治理单位正在从“单个工具是否安全”扩展到“工具、数据流、权限和动作组合是否安全”。

### 2.4 从准确率转向校准与风险覆盖

准确率评分会奖励猜测；OpenAI 的文章建议对自信错误施加高于不确定性的惩罚，并对恰当的不确定性表达给予部分分数。[13] 在智能体场景中，这应进一步展开为风险—覆盖曲线：低风险问题可以自动回答，高风险问题需要更多证据或人工介入，而不是用单一阈值决定所有任务。

## 3. Critical Assessment [Confidence: High for the control principles; Medium for domain-specific thresholds]

### 3.1 先分清失真的根因

建议把每次异常归入以下类别。分类本身就是工程要求，因为不同类别对应不同补救措施。

| 根因 | 典型信号 | 首选处置 | 不应做的事 |
|---|---|---|---|
| 工具选择错误 | 工具与任务实体、动作或作用域不匹配 | 阻断调用，重新规划 | 用自然语言解释掩盖错误工具 |
| 参数错误 | 缺少过滤条件、实体歧义、时间范围错误 | schema 与业务规则校验，必要时追问 | 让模型自行猜测后直接执行 |
| 工具实现错误 | 字段含义漂移、分页错误、统计口径错误 | 标记观察无效，使用独立来源或人工复核 | 把格式正确的结果当作已验证 |
| 上游数据错误 | 来源自身缺失、延迟、口径冲突 | 降低来源可信度，交叉验证 | 仅因工具返回 `ok` 就采信 |
| 传输或截断 | 超时、部分结果、token 截断 | 检查完整性，安全重试或降级 | 把部分数据总结成完整结论 |
| 模型误读 | 字段混淆、否定遗漏、条件范围扩大 | 确定性抽取、逐主张核对 | 只要求“再仔细看看” |
| 时效不一致 | 旧缓存、传播延迟、版本冲突 | 时间戳、TTL、版本令牌、强一致读或等待 | 静默采用最后出现的结果 |
| 上下文污染 | 工具文本含指令或诱导内容 | 隔离不可信内容，权限与策略门禁 | 把工具输出升级为系统指令 |

多智能体失败分类和任务验证研究支持“按边界定位失败”的思路；τ-bench 的最终状态评估支持“不要只看措辞”。[1][2] 具体检测阈值仍需按业务风险校准。

### 3.2 推荐的八层防线

以下是本报告的综合工程框架，不是某一篇论文的固定方案。

#### 第一层：任务契约

在调用工具前，把任务转成机器可检查的契约：目标实体、动作类型、时间范围、地理或业务作用域、允许来源、所需证据等级、是否允许写入、完成条件和禁止事项。实体不明确时先澄清，不把歧义传递给工具。

任务契约还应区分“当前状态”“历史状态”和“预测或推断”。例如“现在是否在线”与“过去一小时是否在线”需要不同查询和证据。

#### 第二层：调用前校验

参数必须经过两类检查：

1. **结构校验**：类型、必填字段、枚举、范围和 schema 一致性。严格函数调用可以承担其中一部分，但仅在支持的模型、配置和 schema 子集下有效。[3]
2. **业务校验**：实体是否存在、动作是否与目标匹配、时间窗口是否合理、权限是否足够、是否会产生不可逆副作用。

业务校验应尽可能由确定性代码完成，而不是让同一个模型再生成一次“检查意见”。对于删除、购买、外发、权限修改等动作，必须在执行前展示目标、参数、影响和证据新鲜度，并根据风险要求人工批准。OWASP 建议最小权限、对高风险动作人工审批，并明确目前没有万无一失的提示注入防护。[4]

#### 第三层：工具适配器与响应契约

每个工具应由适配器返回统一观察信封，而不是把供应商原始 JSON 直接交给模型。建议字段包括：

- `call_id`、工具名和版本；
- 参数哈希或脱敏参数；
- `requested_at`、`observed_at`、`source_as_of`；
- 实体、租户、区域、版本等作用域；
- `status`、`complete`、`truncated`；
- `schema_version`、原始结果引用；
- `source`、`trust_level`、`warnings`；
- 可复现的查询或分页游标。

适配器要检查状态码、错误体、分页、空结果、字段漂移和截断。`{"status":"ok","count":0}` 不应直接解释为“世界上没有记录”；它最多表示某来源在某作用域和时刻返回了零条记录。若查询范围不完整，应标记为不完整观察。

有限重试只适用于瞬时网络错误、超时或可修正的格式问题。重试必须有预算、退避、截止时间和幂等保护；对相互矛盾的事实或疑似上游错误，反复调用同一路径通常不是独立验证。工程文章建议对重试动作使用幂等键或等效机制，并指出验证失败后的 fail-open 或 fail-closed 应由用例风险决定，而不是一律fail-closed。[5]

#### 第四层：观察规范化与来源可信度

模型应消费规范化观察，而不是自由文本 swamp。每条观察应明确：

- 这是原始字段、派生字段还是模型摘要；
- 数据来自哪个来源，是否经过缓存或转换；
- 何时观察、何时生效；
- 是否完整、是否只覆盖部分实体；
- 与既有记忆或本次其他观察是否冲突。

建议为来源维护可信度维度，例如权威性、独立性、历史可靠性、时效和覆盖完整性。可信度是证据权重，不是真值保证。两个工具若读取同一个有问题的上游，不应因为“结果一致”就被视为完全独立。

#### 第五层：独立核验与主张—证据绑定

回答中的每个事实声明应拆成最小可核查主张，并绑定到观察 ID、字段或原文片段。引用质量研究要求同时评估回答正确性和引用支持，说明引用存在本身不够。[6]

核验按风险分级：

- **低风险、可逆**：单个可靠来源加结构校验即可；
- **中风险、影响判断**：使用第二种表示、确定性计算或不同工具复核；
- **高风险、不可逆**：执行前独立核验，必要时人工确认；
- **证据冲突**：不静默合并，进入状态裁决或拒答。

核验器应尽量与原生成器解耦。对推理任务，内在自我纠正缺乏外部反馈时可能失败；因此优先采用外部反馈、规则、计算器、数据库重查或独立模型，而不是让同一模型“再想想”。[7] 这并不意味着独立模型永远正确，而是为了减少共享盲点。

#### 第六层：时效与状态裁决

每个事实至少携带两个时间：**观察时间**和**来源生效时间**。再配合版本、TTL、实体作用域和刷新策略，回答才能写成“截至某时、某版本、某实体”。

缓存策略应同时考虑 TTL 和更新失效；Redis 的 cache-aside 指南明确建议二者结合。[9] 写后验证必须使用适合的一致性路径：DynamoDB 的最终一致读可能返回旧值，而强一致读只支持表和本地二级索引，不支持全局二级索引或流。[10] 因此不能抽象地要求“强一致”，必须检查实际读取路径是否支持。

当两个观察冲突时，建议按以下顺序裁决：实体和作用域是否相同；时间范围是否相同；版本是否可比；来源权威性和独立性；任务要求的是当前值还是历史值。无法裁决时保留冲突并降级。STALE 的结果提醒我们，模型可能接受嵌在问题中的陈旧前提，所以状态裁决不能只依赖自然语言推理。[8]

#### 第七层：回答编译

回答编译器只允许使用核验后的证据包。建议把输出分成三类：

1. **已验证事实**：声明加证据引用；
2. **工具报告但未独立确认**：明确写“工具 X 在时刻 T 报告……”；
3. **未知、冲突或证据不足**：说明缺口和下一步。

推断必须与观察分开，例如“日志显示请求失败”是观察，“因此服务已经宕机”是推断，后者需要额外证据。禁止从空结果推出不存在，从单点样本推出总体，从相关性推出因果，除非任务和证据明确支持。

不确定性应由证据覆盖、来源质量、时效和冲突状态计算或约束，不能让模型用自信语气自行决定。准确性导向评分会鼓励猜测，因此系统应允许并奖励恰当的不确定性表达。[13]

#### 第八层：策略门禁、权限与审计

即使回答正确，动作也可能越权或危险。工具输出应被视为不可信外部内容：OWASP 将间接提示注入描述为外部来源内容导致模型行为被改变，并推荐最小权限、高风险人工审批和不可信内容隔离。[4]

建议默认只读、任务级、短时凭证；密钥不进入模型上下文；文件系统、网络和执行环境按任务沙箱化；外发、删除、购买和管理操作经过策略门禁。审批必须绑定精确目标和参数，不能是模糊的“是否继续”。审计记录应关联请求、策略决定、审批、实际调用、原始结果和覆盖操作。

Google 的智能体安全文档索引描述了隔离沙箱、出站 allowlist、最小权限、短时令牌和人工验证等方向，但检索时完整页面不可读取，因此本报告只把它作为官方文档方向的辅助证据，不引用具体原文。[15]

### 3.3 三个典型场景

以下为设计示例，用来说明防线如何组合，而非真实事件。

#### 场景 A：工具结果本身错误

工具返回：

```json
{"status":"ok","count":0,"scope":"tenant=A","observed_at":"2026-11-24T10:00:00Z"}
```

错误回答：“系统中没有任何记录。”

合规回答路径：

1. 适配器确认结果完整、schema 有效；
2. 模型只提取“来源在 tenant=A、某时刻返回 0 条”；
3. 若任务要求跨租户或当前全量事实，发现作用域不匹配；
4. 使用独立查询或声明证据不足；
5. 最终回答：“工具在 tenant=A 范围返回 0 条；不能据此判断其他租户或当前全量状态。”

若工具有已知统计缺陷，即使 schema 完全正确，也必须标记为不可信观察。

#### 场景 B：模型误读正确结果

原始结果包含 `active=false`、`deleted_at=null`、`plan=legacy`。模型可能把“未删除”误说成“活跃”，或忽略 `legacy` 条件。

防线不是再问一次“你确定吗”，而是：

- 用确定性投影抽取字段；
- 为每个声明绑定字段值；
- 由规则或独立核验器检查“active”是否被错误等同为“未删除”；
- 若任务需要综合判断，明确列出判断规则及其证据。

#### 场景 C：时效或状态不一致

某配置在 10:01 已更新，但 10:02 的缓存仍返回 10:00 版本。若模型直接回答当前值，就会产生事实性错误。

防线是记录 `source_as_of` 和版本，比较任务允许的最大陈旧窗口；必要时失效缓存、使用支持的强一致读或等待传播后重查。若无法确认，回答应限定为“截至 10:00 的缓存值”，而不是“当前值”。DynamoDB 的一致性限制和 Redis 的 TTL/失效建议分别说明了读取路径与缓存策略为什么必须被显式建模。[9][10]

### 3.4 不应依赖的“伪防线”

- **只改提示词**：无法约束工具实现、数据源、缓存和权限边界。
- **只看 schema**：结构正确不代表语义和事实正确。[3]
- **重复调用同一工具**：可能重复同一缓存、同一缺陷或同一上游错误。
- **让模型自我批评**：对外部事实没有新反馈时，可能共享原错误；推理研究已显示内在自我纠正可能退化。[7]
- **有引用就可信**：引用可能不支持主张，或引用本身来自错误来源。[6]
- **最终答案正确就通过**：可能掩盖危险中间动作或错误工具轨迹。[2][11]
- **一律拒答**：会把不确定性治理变成可用性事故；需要按风险校准覆盖与拒答。

### 3.5 成本、风险与可用性权衡

| 目标冲突 | 低成本方案 | 高保障方案 | 决策依据 |
|---|---|---|---|
| 延迟 vs 可靠性 | 单来源、结构校验 | 独立重查、人工确认 | 动作不可逆性、错误代价 |
| 新鲜度 vs 负载 | 较长 TTL、按需刷新 | 事件失效、版本校验 | 数据变化速度、陈旧代价 |
| 自动化 vs 控制 | 自动执行低风险读写 | 高风险审批与硬策略 | 权限范围、资金/数据影响 |
| fail-open vs fail-closed | 有限降级并明确标注 | 安全停止 | 业务连续性 vs 错误后果 |
| 证据留存 vs 隐私 | 脱敏摘要、短期留存 | 加密原始证据、严格访问 | 审计义务与数据敏感性 |

不存在适用于所有领域的统一阈值。系统应把“可靠性预算”写进任务契约，而不是让模型临场决定。

## 4. Action Plan

### P0：先阻止最危险的错误

- [ ] 定义事实声明等级：已验证事实、工具报告、推断、未知；禁止混淆输出。
- [ ] 为所有工具建立统一观察信封，至少包含来源、参数、观察时间、作用域、完整性和状态。
- [ ] 在工具适配器加入 schema、错误体、分页、截断和空结果检查；结构化输出只作为第一道防线。
- [ ] 为每个事实或记忆增加 `source_as_of`、版本和 TTL/刷新规则；回答必须限定时间与实体范围。
- [ ] 对删除、购买、外发、权限变更等动作增加精确参数展示、策略门禁和必要的人工审批。
- [ ] 默认只读和最小权限，工具输出与系统指令隔离，密钥不进入模型上下文。

### P1：建立证据链与独立核验

- [ ] 为回答中的每个事实声明生成 `claim_id -> evidence_id -> raw_result_ref` 映射。
- [ ] 将“字段抽取”与“推断”拆成不同步骤；推断必须声明额外规则和证据。
- [ ] 按风险选择核验方式：规则、确定性计算、独立工具、权威来源或独立模型。
- [ ] 建立冲突裁决规则；无法裁决时保留冲突、限定结论或拒答，不静默覆盖。
- [ ] 重试仅覆盖瞬时错误，设置次数、退避、截止时间和幂等保护；语义错误不盲目重试。
- [ ] 记录完整工具轨迹：调用、参数、原始结果、校验、重试、核验、审批和最终动作。

### P2：用故障注入证明防线有效

- [ ] 建立黄金任务集，覆盖正常、空结果、错误参数、错误工具、上游错误、部分结果和超时。
- [ ] 注入陈旧缓存、版本回退、最终一致读、冲突观察和实体作用域错误。
- [ ] 注入被污染的工具文本，验证其不能改变系统指令或越权调用。
- [ ] 分别评估工具选择、参数正确性、观察有效性、证据覆盖、未支持声明率、陈旧率和冲突识别。
- [ ] 使用正确/错误/拒答结果计算校准误差、不必要拒答率和风险—覆盖曲线。
- [ ] 对工具任务同时检查最终状态和多次运行可靠性，参考 τ-bench 的 `pass^k` 思路。[2]
- [ ] 让新守卫在测试集上至少证伪一次：注入一个它声称能拦住的错误，确认它确实失败或降级。

### P3：持续运营

- [ ] 按工具、来源和任务类型监控异常率、重试率、陈旧率、冲突率和拒答率。
- [ ] 对高风险动作抽样人工复核，并复盘“答案正确但过程错误”的案例。
- [ ] 定期更新来源可信度、TTL 和权限；工具版本变化时重跑回归集。
- [ ] 将安全与可靠性评测纳入发布门禁，而不是只做演示性测试。

## 5. Open Questions & Caveats

1. **没有通用准确率提升数字。** 不同模型、工具、领域和风险等级差异很大，本报告不声称某套提示词能降低某个固定百分比幻觉。
2. **阈值需要领域校准。** 多长时间算陈旧、多少来源足够、何时必须人工审批，都取决于数据变化速度、错误代价和可用性要求。
3. **独立性难以完全保证。** 多个工具可能共享同一上游或同一模型盲点；需要在来源拓扑层面评估，而不是只数工具数量。
4. **自我纠正结论有范围限制。** 关键论文针对推理任务，不能直接外推为所有事实核验场景的效应量。[7]
5. **STALE 是预印本。** 55.2% 只适用于其特定基准，不应作为通用智能体能力指标。[8]
6. **部分安全资料不完整。** Google 智能体安全页面检索时只能读取索引摘要，因此只作方向性参考。[15]
7. **故障注入论文记录不完整。** ToolMaze 的 AMiner 页面无法提供完整作者、摘要和全文；只保留“可能过度信任损坏输出”的低置信度方向性证据，不引用未经核验的循环结论。[12]
8. **安全与可靠性可能冲突。** 更多日志有利于审计但增加隐私风险，更多审批降低风险但增加延迟和审批疲劳，需要显式预算。

## Methodology

本次采用 standard 深度。先确认用户范围是通用治理框架，优先覆盖工具结果错误、模型误读和时效/状态不一致。研究分为六个领域：失真根因、运行时防线、回答接地、时效与状态、评估与可观测性、治理与安全。

第一轮由三个检索子代理并行执行，共获得 15 个来源；每个领域至少有两个来源，并包含至少一个失败或批评视角，因此未启动第二轮补检。主代理按 URL 去重并重新编号，未发现重复来源。随后由一个独立核验代理检查 10 组关键结论与来源。

核验修正包括：严格函数调用补充支持模型、配置和 schema 条件；自我纠正结论限定在推理任务；fail-closed 改为按业务风险选择；DynamoDB 强一致读补充表和本地二级索引限制；STALE 标注预印本与基准范围；ToolMaze 因全文不可获取而降低为 snippet-only、低置信度证据。原计划按六个领域平铺，最终合并为“现状—趋势—批判性评估—行动—开放问题”，以避免运行时防线与安全治理重复叙述；结构变化小于 50%。

## Bibliography

[1] Mert Cemri et al. — *Why Do Multi-Agent LLM Systems Fail?* — https://arxiv.org/abs/2503.13657 — 2025-03-17，修订 2025-10-26 — 访问 2026-11-24 — Tier: 1

[2] Shunyu Yao, Noah Shinn, Pedram Razavi, Karthik Narasimhan — *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains* — https://arxiv.org/abs/2406.12045 — 2024-06-17 — 访问 2026-11-24 — Tier: 1

[3] OpenAI Help Center — *Function Calling in the OpenAI API* — https://help.openai.com/en/articles/8555517-function-calling-in-the-openai-api — 日期未标注 — 访问 2026-11-24 — Tier: 1

[4] OWASP GenAI Security Project — *LLM01:2025 Prompt Injection* — https://genai.owasp.org/llmrisk/llm01-prompt-injection/ — 2024-04-10，修改 2025-04-17 — 访问 2026-11-24 — Tier: 1

[5] Aman Sardana / IEEE Computer Society — *When AI Fails: Engineering Fault-Tolerant Mission-Critical Systems* — https://www.computer.org/publications/tech-news/trends/fault-tolerance-for-ai — 2026-10-30 — 访问 2026-11-24 — Tier: 2（工程评论，非标准）

[6] Tianyu Gao, Howard Yen, Jiatong Yu, Danqi Chen — *Enabling Large Language Models to Generate Text with Citations* — https://arxiv.org/abs/2305.14627 — EMNLP 2023 — 访问 2026-11-24 — Tier: 1

[7] Jie Huang et al. — *Large Language Models Cannot Self-Correct Reasoning Yet* — https://arxiv.org/abs/2310.01798 — ICLR 2024 — 访问 2026-11-24 — Tier: 1

[8] Hanxiang Chao, Yihan Bai, Rui Sheng, Tianle Li, Yushi Sun — *STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?* — https://arxiv.org/abs/2605.06527 — 2026-05-07 — 访问 2026-11-24 — Tier: 1（预印本）

[9] Redis Documentation — *Redis cache-aside* — https://redis.io/docs/latest/develop/use-cases/cache-aside/ — 日期未标注 — 访问 2026-11-24 — Tier: 1

[10] Amazon Web Services — *DynamoDB read consistency* — https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html — 日期未标注 — 访问 2026-11-24 — Tier: 1

[11] Google Cloud / Google for Developers — *From “gut feel” to data-driven: evaluating multi-agent systems* — https://codelabs.developers.google.com/codelabs/production-ready-ai-roadshow/2-evaluating-multi-agent-systems?hl=en — 日期未标注 — 访问 2026-11-24 — Tier: 1

[12] AMiner academic index，作者未暴露 — *When Tools Fail: Benchmarking Dynamic Replanning and Anomaly Recovery in LLM Agents* — https://www.aminer.cn/pub/6a2221d70a96f8c83c4d1164/when-tools-fail-benchmarking-dynamic-replanning-and-anomaly-recovery-in-llm-agents — 日期未核实 — 访问 2026-11-24 — Tier: 3（snippet-only）

[13] OpenAI — *Why language models hallucinate* — https://openai.com/index/why-language-models-hallucinate/ — 2025-09-05 — 访问 2026-11-24 — Tier: 1

[14] OWASP GenAI Security Project — *2025 Top 10 Risk & Mitigations for LLMs and Gen AI Apps* — https://genai.owasp.org/llm-top-10/ — 2025 版 — 访问 2026-11-24 — Tier: 1

[15] Google AI for Developers — *Agents overview: security, credentials, and human oversight* — https://ai.google.dev/gemini-api/docs/agents?hl=en — 日期未标注 — 访问 2026-11-24 — Tier: 1（官方文档索引，snippet-only）

## Source Extracts

### [1] Why Do Multi-Agent LLM Systems Fail?

- **Summary:** 基于 1,600+ 标注轨迹和七种框架，提出 14 类失败模式，覆盖系统设计、智能体间错位和任务验证。
- **Key quotes:** “14 unique modes, clustered into 3 categories”；“task verification”。
- **Source type:** 学术论文。
- **Credibility tier:** 1。

### [2] τ-bench

- **Summary:** 用领域 API、政策、模拟用户和最终数据库状态评价工具智能体，并通过多次运行衡量可靠性。
- **Key quotes:** “compares the database state at the end of a conversation with the annotated goal state”；“pass^8 <25% in retail”。
- **Source type:** 学术论文/基准。
- **Credibility tier:** 1。

### [3] OpenAI Function Calling

- **Summary:** 严格模式在支持条件下约束参数符合 schema；不能据此推断语义或事实正确。
- **Key quotes:** “setting strict: true in your function definition ensures that the generated arguments adhere to the supplied schema”。
- **Source type:** 官方文档。
- **Credibility tier:** 1。

### [4] OWASP LLM01:2025

- **Summary:** 描述直接和间接提示注入，推荐最小权限、高风险人工审批、不可信内容隔离和对抗测试，并提醒没有万无一失方案。
- **Key quotes:** “Restrict the model’s access privileges to the minimum necessary for its intended operations”；“Separate and clearly denote untrusted content to limit its influence on user prompts。”
- **Source type:** 安全权威指南。
- **Credibility tier:** 1。

### [5] When AI Fails

- **Summary:** 工程评论主张把语义验证、grounding、校准、有限重试、幂等、熔断和安全降级纳入容错系统；fail-open/fail-closed 应按风险选择。
- **Key quotes:** “Passing validation does not guarantee that an answer is correct”；“retried actions need idempotency keys or another explicit mechanism to prevent duplicate effects”。
- **Source type:** 工程评论。
- **Credibility tier:** 2，非 IEEE 标准。

### [6] Enabling Large Language Models to Generate Text with Citations

- **Summary:** ALCE 同时评估回答和引用质量；引用支持不完整是显著问题。
- **Key quotes:** “even the best models lack complete citation support 50% of the time.”
- **Source type:** 学术论文。
- **Credibility tier:** 1。

### [7] Large Language Models Cannot Self-Correct Reasoning Yet

- **Summary:** 对推理任务，缺乏外部反馈时内在自我纠正不可靠，可能降低性能；支持优先外部反馈，但不能无限外推。
- **Key quotes:** “LLMs struggle to self-correct their responses without external feedback, and at times, their performance even degrades after self-correction.”
- **Source type:** 学术论文。
- **Credibility tier:** 1。

### [8] STALE

- **Summary:** 研究隐含记忆冲突和陈旧前提；最佳受测模型在该基准总体准确率 55.2%。
- **Key quotes:** “a pervasive gap between retrieving updated evidence and acting on it”；“even the best evaluated model achieving only 55.2% overall accuracy.”
- **Source type:** 学术预印本。
- **Credibility tier:** 1，预印本。

### [9] Redis cache-aside

- **Summary:** 建议 TTL 限制陈旧窗口，并在更新主库时失效缓存键；多实例失效仍需工程治理。
- **Key quotes:** “with a TTL that bounds how long stale data can be served.”
- **Source type:** 官方文档。
- **Credibility tier:** 1。

### [10] DynamoDB read consistency

- **Summary:** 最终一致读可能不反映最近写入；强一致读支持表和本地二级索引，不支持全局二级索引或流。
- **Key quotes:** “the responses might not reflect the results of a recently completed write operation.”
- **Source type:** 官方文档。
- **Credibility tier:** 1。

### [11] Evaluating multi-agent systems

- **Summary:** 区分能力和质量评估，覆盖工具选择、参数、顺序、groundedness 与人工评审。
- **Key quotes:** 未使用直接引文。
- **Source type:** 官方工程指南。
- **Credibility tier:** 1。

### [12] When Tools Fail / ToolMaze

- **Summary:** 可访问摘要片段提到对损坏输出的过度信任；全文、作者和完整结论不可读取，因此仅作方向性证据。
- **Key quotes:** “Driven by systemic over-trust in corrupted outputs”。
- **Source type:** 学术索引片段。
- **Credibility tier:** 3，snippet-only。

### [13] Why language models hallucinate

- **Summary:** 准确性评分鼓励猜测；建议重罚自信错误并奖励恰当的不确定性表达。
- **Key quotes:** “Penalize confident errors more than you penalize uncertainty, and give partial credit for appropriate expressions of uncertainty.”
- **Source type:** 研究/工程文章。
- **Credibility tier:** 1。

### [14] OWASP 2025 Top 10

- **Summary:** 将提示注入、信息泄露、供应链、输出处理、过度代理、错误信息和无界消耗等风险分别列出，支持分层治理。
- **Key quotes:** 未使用直接引文。
- **Source type:** 安全权威指南。
- **Credibility tier:** 1。

### [15] Google Agents security overview

- **Summary:** 检索摘要提及隔离沙箱、网络 allowlist、最小权限、短时令牌和人工验证；完整页面不可读取，不引用原文。
- **Key quotes:** 无。
- **Source type:** 官方文档索引。
- **Credibility tier:** 1，snippet-only。
