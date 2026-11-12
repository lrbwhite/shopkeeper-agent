<div align='center'>
  <h1 style="margin-top: 15px;">「电商问数」智能数据分析 Agent</h1>
  <h4><b>shopkeeper-agent</b></h4>
  <p><em>基于 LangGraph 的多阶段 Text-to-SQL Agent：效果可评测、链路可终止、索引可重建、外部依赖可容错</em></p>
</div>

<div align='center'>

![AI](https://img.shields.io/badge/AI-Agent-00c853?style=flat)
![Python](https://img.shields.io/badge/Python-3.14-3776AB.svg?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-Agentic%20Workflow-1C3C3C.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688.svg)
![Eval](https://img.shields.io/badge/Evaluation-20%20Questions-blue.svg)

[项目定位](#position) • [改造亮点](#features) • [评测结果](#evaluation) • [系统架构](#architecture) • [快速开始](#quick-start) • [已知问题](#known-issues) • [后续规划](#roadmap)

</div>

> **项目来源**：本项目基于开源教程项目 [didilili/shopkeeper-agent](https://github.com/didilili/shopkeeper-agent) 二次开发。原项目已提供电商数仓、元数据知识库，以及「三路召回 → SQL 生成/校验/修正/执行」的完整链路；本仓库在此基础上完成评测体系、编排可控化、索引幂等、关键词扩展合并和云端接入适配，并用同一套评测集记录改造前后的差异。

![电商问数前端首页：样例问题、自然语言输入和智能数据分析 Agent 界面](docs/images/shopkeeper-agent-home.jpg)

<a id="position"></a>

## 项目定位

真实问数场景里，业务同学不会写 SQL，也很难记住表结构、字段含义、指标口径和字段取值。直接把自然语言问题丢给大模型，常见问题是选错表、选错字段、理解错指标和 SQL 幻觉。

这个项目围绕电商数仓解决这条链路：用户用自然语言提问，系统先召回相关字段、指标和字段取值，再由大模型分步推理生成 SQL，校验并执行后以流式方式返回结果。

**个人工作范围**：不重写原项目的执行链路，而是把「一个能跑通的 Demo」改造成「可量化、可回归、可排障的工程项目」，具体包括评测体系、编排可控化、元数据构建幂等、关键词扩展合并与公网依赖容错。

<a id="features"></a>

## 改造亮点

### 1. 可评测：双通道评测脚本 + 20 题评测集

- [`evaluation/questions.jsonl`](evaluation/questions.jsonl)：20 道题，覆盖单表聚合、多表 Join、指标口径、字段取值过滤、相对时间 5 类；每题标注期望字段、指标、取值、表和 SQL 关键词。
- [`app/scripts/evaluate.py`](app/scripts/evaluate.py)：两条互不干扰的评测通道
    - **节点级（recall）**：绕过整张图，依次执行关键词抽取、扩展和三路召回，输出覆盖率、命中率、平均候选数、异常数；
    - **端到端（e2e）**：跑完整 graph，输出执行成功率、P50/P95 延迟、平均 LLM 调用次数、平均 token、SQL 关键词覆盖率、表命中覆盖率与失败题号。
- 一题一会话、三路召回独立计分，避免「端到端错了但不知道错在召回还是 SQL」。
- 报告附带 git commit、模型名与 embedding 模型；`--baseline` 会逐项打印与基线报告的 delta。
- 单题失败（扩展节点超时、召回异常）只记录 `error` 并继续，不会让整轮 20 题作废。

### 2. 成本治理：三路关键词扩展合并为单次结构化输出

原实现中 `recall_column` / `recall_metric` / `recall_value` 各自调用一次 LLM 做关键词扩展，共 3 次，且三路语义可能互相矛盾。改造后：

- 新增 [`suggest_keywords`](app/agent/nodes/suggest_keywords.py) 节点，一次调用同时产出 `column_keywords` / `metric_keywords` / `value_keywords`；
- 三路召回节点只读 state 中各自的词表，不再调用 LLM；
- 结构化输出使用 `with_structured_output(..., method="json_mode")`：DashScope 网关对工具调用的 json schema 偶发 `grammar compile timed out`（400，SDK 不重试），`json_mode` 绕开了该问题；
- 单题关键词扩展调用由 3 次降为 1 次，整链路 LLM 调用由 6 次降为 4 次，单题 token 下降约 15%。

> 该改造目前**已搁置**：合并后模型对「哪些字段/指标是必需的」约束变弱，需要逐类补强制项，详见 [`plan/P1-1_改造总结.md`](plan/P1-1_改造总结.md)。

### 3. 可控：SQL 重试上限 + 失败降级 + 节点级 trace

- [`app/agent/graph.py`](app/agent/graph.py) 为 SQL 修正设置 `MAX_SQL_RETRY = 2`，修正次数不足才进入 `correct_sql`，超限后走失败出口，链路一定终止。
- [`handle_sql_failure.py`](app/agent/nodes/handle_sql_failure.py)：重试耗尽后把「最后一条 SQL + 数据库错误」包装成可读消息返回前端，而不是抛异常打断 SSE 流。
- `traced` 装饰器统一包裹所有节点，记录 `node / status / latency_ms / error`：既追加到 `state.trace` 供评测报告使用，也通过 SSE 的 `trace` 事件实时输出。
- 修正后的 SQL 必须重新进入 `validate_sql`，保证「重试上限」真实生效。

### 4. 可重建：元数据构建幂等 + 全链路对账

原始构建脚本的可重入性差：Qdrant point 使用 `uuid4()`，重跑会追加重复点；MySQL 重复写入触发主键冲突；ES 中途失败会留下半个索引。改造后：

- **Qdrant**：point id 改为 `uuid5(NAMESPACE, "类型:id:视角")` 稳定 ID，配合 upsert 实现「同 id 整点覆盖」。
- **MySQL**：批量写入改为 `INSERT ... ON DUPLICATE KEY UPDATE`，重复构建按主键覆盖。
- **ES**：文档 id 本身就是 `column_id.value`，天然稳定，保持不变。
- **构建后对账**：按「本次构建的 id 集合」与存储实际数据取差集清理——MySQL 用 `NOT IN` 删行，Qdrant 用 `scroll` 列举后按 id 删点，ES 用 `scroll` + `bulk` 删除。

验证方式：连续执行两次构建脚本，MySQL 行数、Qdrant points、ES docs 数保持一致，第二次对账清理数为 0；手工塞入过期数据后重跑，可被精确清除。

### 5. 可容错：公网依赖的失败面收敛

- [`retry.py`](app/repositories/qdrant/retry.py)：仅对 `ResponseHandlingException` / `TransportError` 等传输异常做 3 次指数退避（0.5s / 1s / 2s），响应解析放在重试之外。
- LLM 客户端：`max_retries=3`、`request_timeout=180`。该模型为推理模型，单次 reasoning 输出 2k~2.7k token，60 秒超时会被误杀（实测一次 245 秒 = 4 次尝试 × 60 秒 + 退避）。
- Embedding 迁移到 OpenAI 兼容云端接口，去掉本地模型下载与挂载；Docker 依赖从 5 个服务降到 1 个（仅 Elasticsearch）。

<a id="evaluation"></a>

## 评测结果

> 20 题属于小样本，绝对值会随模型服务波动。报告保留 git commit 与模型名，目的是**在同一配置下**做改造前后对比，而不是横向刷榜。工作区存在未提交改动时，报告里的 commit 不能代表实际代码。

### 当前结果（P1-1 改造后，commit `1b4aa8c`）

| 指标 | 结果 | 说明 |
| --- | --- | --- |
| 字段召回覆盖率 | **0.9833** | 20 题全量，命中率 1.00 |
| 指标召回覆盖率 | 0.8571 | 仅 q005 一题未命中；同义扩展规则已写入 prompt，待验证 |
| 取值召回覆盖率 | 1.00 | — |
| 平均候选数（字段 / 指标 / 取值） | 6.8 / 0.95 / 0.35 | 字段候选较基线 4.8 上升，噪声待 P1-3 处理 |
| 端到端成功率 | 1.00 | 4 题抽样（`after_p1_1_e2e_limit4.json`） |
| 平均 LLM 调用次数 / 题 | **4.0** | 改造前 6 次 |
| 平均 token / 题 | **6,134** | 同 4 题改造前 7,222，下降约 15% |
| P50 / P95 延迟（4 题） | 54.0s / 56.3s | 样本太小，不作结论 |

### 阶段对比

| 阶段 | commit | 字段覆盖率 | 指标 / 取值 | 端到端成功率 | 调用/题 | token/题 |
| --- | --- | --- | --- | --- | --- | --- |
| 初始基线 | `0ab1410` | 0.8778 | 1.00 / 1.00 | 0.50 | 4.5 | 5,354 |
| P1-1 改造前 | `918734f` | 0.9583 | 1.00 / 1.00 | 0.95 | 5.85 | 7,688 |
| P1-1 改造后 | `1b4aa8c` | 0.9833 | 0.8571 / 1.00 | 4 题 1.00 | 4.0 | 6,134 |

> 初始基线的成功率 0.5、调用数偏低，是因为索引重复写入与缺少重试导致题目提前失败，不能直接与后续阶段比较。阶段之间还切换过模型与向量库接入方式，跨版本差异包含环境因素。

### 评测命令

```bash
# 全量：召回 + 端到端
uv run python -m app.scripts.evaluate --mode all

# 只跑召回，并与基线对比
uv run python -m app.scripts.evaluate --mode recall --baseline evaluation/before_p1_1_recall.json

# 只跑端到端，并与基线对比
uv run python -m app.scripts.evaluate --mode e2e --baseline evaluation/before_p1_1_e2e.json

# 小样本快速回归
uv run python -m app.scripts.evaluate --mode recall --limit 4
```

常用参数：`--mode recall|e2e|all`、`--limit N`、`--questions <jsonl>`、`--output <path>`、`--baseline <path>`。

<a id="architecture"></a>

## 系统架构

![电商问数系统架构图：前端通过 FastAPI 和 SSE 连接后端，LangGraph 问数智能体基于 Jieba、MySQL、Qdrant、Elasticsearch 和 LLM 完成召回、SQL 生成校验执行与结果返回](docs/images/shopkeeper-agent-system-architecture.svg)

| 主线 | 做什么 | 涉及模块 |
| --- | --- | --- |
| 元数据知识库构建 | 抽取数仓中的表、字段、指标和字段取值，幂等写入结构化库、向量库和全文索引，并对账清理旧数据 | `MySQL` / `Qdrant` / `Elasticsearch` / `Embedding` |
| 自然语言问数 | 基于用户问题完成召回、上下文整理、SQL 生成校验执行，并把节点进度与耗时流式返回前端 | `LangGraph` / `FastAPI` / `SSE` / `React` |

### 问数工作流

```text
用户问题
  → 关键词抽取（jieba + 词性过滤）
  → 三路词表扩展（单次结构化输出：字段 / 指标 / 取值）
  → 三路并行召回：字段（Qdrant）/ 指标（Qdrant）/ 字段取值（ES）
  → 召回结果合并 + 指标依赖字段补齐 + 取值合并进字段 examples + 主外键补齐
  → 候选表过滤 / 指标过滤
  → 日期、星期、季度与数据库方言上下文补齐
  → SQL 生成 → EXPLAIN 校验 ─┬─ 通过 → 执行 SQL → SSE 返回结果
                              ├─ 失败且 retry_count < 2 → SQL 修正 → 重新校验
                              └─ 重试耗尽 → 失败降级：返回最后一条 SQL 与可读错误
```

![电商问数查询结果页：LangGraph 执行流程、SQL 校验执行和查询结果表格](docs/images/shopkeeper-agent-query-result.jpg)

### 技术栈

| 模块 | 技术 | 作用 |
| --- | --- | --- |
| 数仓 | `MySQL` | 模拟事实表、维度表和分析型查询环境 |
| 元数据库 | `MySQL` / `SQLAlchemy` | 保存表、字段、指标、字段指标关系等结构化元数据 |
| 向量检索 | `Qdrant Cloud` | 保存字段和指标向量，支持语义召回 |
| 全文检索 | `Elasticsearch` | 保存字段真实取值，支持字面命中 |
| Embedding | OpenAI 兼容接口（阿里云百炼） | 文本转向量，无需本地部署模型 |
| 智能体编排 | `LangGraph` | 固定 DAG 工作流，含重试上限、失败降级与节点 trace |
| 模型接入 | `LangChain` / `langchain-openai` | 统一模型调用，内置重试与超时 |
| 后端接口 | `FastAPI` | 问数 API、依赖注入和生命周期管理 |
| 流式协议 | `SSE` | 实时返回 `progress` / `trace` / `result` / `error` |
| 质量评测 | 自建评测脚本 + 20 题评测集 | 节点级召回指标、端到端指标与基线对比 |
| 前端 | `React` / `Vite` / `Tailwind CSS` | 聊天式问数界面与流程展示 |
| 日志追踪 | `ContextVar` / `loguru` | 为并发请求注入 request_id |
| 依赖管理 | `uv` / `pnpm` | Python 后端与前端依赖 |

### 项目结构

```text
shopkeeper-agent/
├── app/
│   ├── agent/            # LangGraph 图、状态、运行时上下文、trace 装饰器与各节点
│   ├── api/              # FastAPI 路由、依赖注入、生命周期与请求结构
│   ├── clients/          # MySQL、Qdrant、Elasticsearch、Embedding 客户端管理
│   ├── conf/             # 配置 dataclass 与加载工具
│   ├── core/             # 日志、request_id 上下文
│   ├── entities/         # 业务实体（仓储层出口统一转换为实体）
│   ├── models/           # SQLAlchemy ORM 模型
│   ├── prompt/           # Prompt 加载工具
│   ├── repositories/     # MySQL / Qdrant / ES 数据访问层（幂等对账、重试）
│   ├── scripts/          # 元数据构建脚本、离线评测脚本
│   └── services/         # 元数据构建服务、问数编排服务
├── conf/                 # app_config.yaml、meta_config.yaml
├── docker/               # Docker Compose（仅 ES）、MySQL 初始化 SQL
├── docs/images/          # README 截图与架构图
├── evaluation/           # 评测集与各阶段报告
├── frontend/             # React + Vite + Tailwind CSS 前端
├── plan/                 # 改造计划与阶段性总结
├── prompts/              # 关键词扩展、过滤、SQL 生成/修正等 Prompt
├── main.py               # FastAPI 应用入口
└── pyproject.toml        # 依赖与工具配置
```

<a id="quick-start"></a>

## 快速开始

### 1. 环境要求

- Python `>= 3.14` 与 [`uv`](https://docs.astral.sh/uv/)
- MySQL `8.0`（本地安装，非 Docker）
- Docker（仅用于启动 Elasticsearch）
- Node.js 与 `pnpm`（前端）
- 阿里云百炼 API Key（LLM 与 Embedding 共用）
- Qdrant Cloud 集群地址与 API Key

### 2. 克隆并安装依赖

```bash
git clone https://github.com/lrbwhite/shopkeeper-agent.git
cd shopkeeper-agent
uv sync
```

### 3. 配置密钥

```bash
cp .env.example .env
```

```bash
DASHSCOPE_API_KEY=your_dashscope_api_key
QDRANT_API_KEY=your_qdrant_api_key
```

再确认 [`conf/app_config.yaml`](conf/app_config.yaml)：

- `qdrant.url`：Qdrant Cloud 集群地址
- `llm.model_name` / `llm.base_url`：OpenAI 兼容的模型服务
- `embedding.base_url` / `embedding.model`：OpenAI 兼容的 Embedding 服务
- `db_meta` / `db_dw`：本地 MySQL 连接信息

### 4. 初始化 MySQL

先在本地 MySQL 创建账号（默认配置为 `didilili / dili123`，也可以改配置）：

```sql
CREATE USER IF NOT EXISTS 'didilili'@'localhost' IDENTIFIED BY 'dili123';
```

然后导入两个初始化脚本。脚本负责建库与授权，不会自动创建用户：

```bash
mysql -u root -p < docker/mysql/meta.sql
mysql -u root -p < docker/mysql/dw.sql
```

### 5. 启动 Elasticsearch

```bash
docker compose -f docker/docker-compose.yaml up -d
```

Compose 中只保留 Elasticsearch（`9200`）；MySQL 本地安装，Qdrant 与 Embedding 使用云端服务，Kibana 与 Qdrant/MySQL 容器均已注释。

### 6. 构建元数据知识库

```bash
uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml
```

脚本会把表字段元数据写入 MySQL、字段和指标向量写入 Qdrant、字段真实取值写入 Elasticsearch，并在构建结束后对三处存储做差集对账。

> 构建是幂等的，可连续执行两次验证：MySQL 行数、Qdrant points、ES docs 数量应保持一致。

### 7. 离线评测（可选）

```bash
uv run python -m app.scripts.evaluate --mode all
```

报告默认写入 `evaluation/report_YYYYMMDD_HHMMSS.json`。

### 8. 启动后端

```bash
uv run fastapi dev main.py
```

接口：

```text
POST http://127.0.0.1:8000/api/query
```

请求示例：

```json
{
    "query": "统计华北地区的销售总额"
}
```

SSE 消息类型：

| 类型 | 含义 |
| --- | --- |
| `progress` | 节点执行进度 |
| `trace` | 节点耗时、状态与错误 |
| `result` | 最终查询结果 |
| `error` | 全局异常或 SQL 重试耗尽消息 |

### 9. 启动前端

```bash
cd frontend
pnpm install
pnpm dev
```

前端默认通过 Vite 代理把 `/api` 转发到 `http://127.0.0.1:8000`。如需修改：

```bash
cd frontend
cp .env.example .env
```

```bash
VITE_DEV_PROXY_TARGET=http://127.0.0.1:8000
```

<a id="known-issues"></a>

## 已知问题

- **指标召回 q005**：期望指标 GMV 未被召回，指标覆盖率 0.8571。修复规则（有度量意图时至少给出原表达 + 同义/缩写）已写入 prompt，尚未验证、未提交。
- **字段候选噪声**：平均候选数从 4.8 涨到 6.8，覆盖率达标但会挤占后续上下文，计划在 P1-3 用 payload 过滤 / RRF 重排处理。
- **全量端到端未复跑**：P1-1 后只有 4 题样本，P50/P95 与全量成功率待补。
- **三路扩展合并的约束衰减**：合并为单次调用后，模型对「必需字段/指标」的纪律弱于三份专用 prompt，需要逐类补强制项，详见 [`plan/P1-1_改造总结.md`](plan/P1-1_改造总结.md)。

<a id="roadmap"></a>

## 后续规划

按评测结果的短板排序：

1. **召回批量化 + 并发化（P1-2）**：召回节点内部仍是逐关键词串行 `aembed_query` + `search`，改为关键词去重后批量向量化、`asyncio.gather` 并发检索并配 `Semaphore` 控制并发。
2. **检索增强（P1-3）**：Qdrant payload 过滤（按业务域/表角色预过滤）、向量与关键词结果 RRF 融合、top-20 精排取 top-5。
3. **检索缓存（P1-4）**：精确缓存与语义缓存，带过期策略并记录命中率。
4. **SQL 安全**：只读白名单校验，只允许 `SELECT`。
5. **交互能力**：多轮会话改写、前端 trace 与召回过程可视化。

本仓库不覆盖生产治理能力，例如登录与数据权限、多租户隔离、SQL 审计、监控告警、灰度发布。

## 简历表述

> **电商数仓问数 Agent（基于开源 LangGraph 项目二次开发）**
>
> - 基于 LangGraph 编排「关键词抽取 → 三路并行召回 → 上下文合并 → SQL 生成/校验/修正/执行」的固定 DAG 问数链路；新增 SQL 修正上限 2 次与失败降级节点，避免错误 SQL 无限重试或异常中断流式响应。
> - 自建 20 题评测集与双通道评测脚本（节点级召回 + 端到端执行），覆盖召回覆盖率/命中率、执行成功率、P50/P95 延迟、LLM 调用数与 token 消耗，支持与基线报告自动 diff；字段召回覆盖率 95.8% → 98.3%，指标/取值召回到达 100%。
> - 将字段、指标、取值三路关键词扩展合并为单次结构化输出，单题 LLM 调用 6 → 4 次、token 下降约 15%，并消除三路语义不一致。
> - 元数据构建改造为幂等可重入：Qdrant 使用 uuid5 稳定 ID + upsert、MySQL 使用 `ON DUPLICATE KEY UPDATE`，构建后对三处存储做差集对账，解决索引膨胀、主键冲突和残留脏数据。

## License

[MIT](LICENSE)
