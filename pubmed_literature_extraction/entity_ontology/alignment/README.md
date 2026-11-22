# Ontology Alignment MVP

把已有 Ontology 和 Neo4j 实体连接起来，输出一张可追溯的映射表。此模块独立于 Ontology Construction Pipeline；仅查询 Neo4j、读取已有图文件、写入本地结果，不修改任一图，不创建新类别。

## 处理流程与复用模块

| 阶段 | 实现 | 作用与复用 |
| --- | --- | --- |
| 数据读取 | `loading.py` | 读取已有 `placement.json`，保留节点、定义、别名与 `IS_A` 父节点；通过 Neo4j 驱动的读事务执行固定查询，保留原始属性、数据库名、element ID、标准 ID 和物种信息。 |
| 候选检索 | `candidates.py` | 先过滤实体类型，再查标准 ID、名称及别名；未命中时使用词项相似度，可选复用 `concepts/embedding.py` 获取语义候选。默认每个 KG 实体最多 6 个候选。 |
| LLM 判断 | `decisions.py`、`pipeline.py` | 复用 `model.py` 的服务配置与 `hierarchy/ranking_journal.py` 的调用日志，判断同一概念、个体归属或不匹配，并检查引用是否出自真实输入。 |
| 外部核查 | 现有 OLS 模块 | 复用 `OLSReferenceAgent`、`EntityReferenceLookup` 及已有 OLS 客户端，查询定义、同义词和层级；默认最多核查 2 个待确认候选，每次 Agent 有最多 5 步工具预算。 |
| 本地导出 | `pipeline.py` | 导出 JSON、CSV、逐实体记录、统计与输入快照，保留候选、模型判断、引用和外部查询依据。 |

输入必须是已经构建好的 Ontology 图。当前 `entity-100-ols-agent-v1` 的 `placement.json` 中有 370 个已放置概念及 7 个类型根节点；加载器不会把文件中额外的 `source_concepts` 当作已放置节点。检索排除类型根节点，其他节点无论是不是叶节点都可以成为候选。`--equivalence` 可按相同节点 ID 补充现有概念信息，不添加新节点。

## 安装与配置

在仓库根目录运行，使用现有 Python 虚拟环境：

```bash
.venv/bin/python -m pip install -r entity_ontology/requirements.txt
.venv/bin/python -m pip install -r entity_ontology/alignment/requirements.txt
```

Neo4j 密码只通过环境变量提供。下面的 `read` 会等待输入且不在终端显示密码：

```bash
export NEO4J_URI="bolt://100.104.181.96:7687"
export NEO4J_USER="neo4j"
read -rs NEO4J_PASSWORD
export NEO4J_PASSWORD
```

目标数据库必须显式填写。本项目中预期的整合知识图谱位于 `liver-kg-core-v02`；同一服务器的默认 `neo4j` 数据库有另一套数据，不能混用。

LLM 沿用已有 `audit_edges` 阶段配置。若已有可用配置，可直接传给 `--config`；否则复制示例到新的本地文件：

```bash
cp entity_ontology/alignment/config.example.json alignment.local.json
export LIVERKG_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY
```

上面的地址是 DashScope 兼容接口示例；按账号地域和服务商填写实际 API 地址，并在 `alignment.local.json` 中选择可用模型。密钥不要写进配置文件。开启 Embedding 时，现有封装要求 LLM 与 Embedding 使用相同服务地址和密钥环境变量。

## 三步运行

先检查目标库的实体数量和字段：

```bash
.venv/bin/python -m entity_ontology.alignment inspect \
  --database liver-kg-core-v02
```

再读取小规模真实样本，默认覆盖七种类型，每种最多 5 条。采样按应用 ID、element ID 排序，便于重现；它不是随机样本。已有快照不会被覆盖。

```bash
.venv/bin/python -m entity_ontology.alignment snapshot \
  --database liver-kg-core-v02 \
  --limit-per-type 5 \
  --output runs/alignment/my-input/kg_snapshot.json
```

最后对本地快照运行 LLM 对齐。下面启用 Embedding，并使用默认开启的 OLS 核查：

```bash
.venv/bin/python -m entity_ontology.alignment run \
  --ontology runs/entity_ontology/entity-100-ols-agent-v1/incremental-placement/placement.json \
  --kg-snapshot runs/alignment/my-input/kg_snapshot.json \
  --config alignment.local.json \
  --output runs/alignment/my-run \
  --embedding \
  --max-llm-calls 200
```

常用选项：

- `snapshot --types Disease Gene Protein`：只采样指定实体类型。
- `run --retrieval-only`：只做本地候选检索，不调用 LLM、OLS 或 Embedding。有候选的结果保留为 `REVIEW_REQUIRED`，不能视为成功对齐。
- `run --no-ols`：跳过外部核查，缺乏证据的结果继续等待复核。
- `run --max-candidates 6 --max-reference-candidates 2`：限制送给模型及外部核查的候选数量。
- `run --max-llm-calls 200`：限制日志中允许的模型调用数量，包含对齐判断与 OLS Agent 决策；不包含 Embedding 请求。
- `run --alignment-prompt verified`：使用当前默认提示词；`legacy` 保留旧版本用于复现。切换版本须换用新的结果目录。

输入和配置相同可以复用同一输出目录中的调用缓存。更换输入、阈值、模型或提示词时使用新的输出目录，避免混合不同实验。输入文件与结果目录应分开。

模型返回格式错误或引用了错误来源时，最多请求一次格式/引用纠正；原回答和纠正回答都保留。纠正后仍不能核实就记为待复核，不放宽证据条件。外部 OLS 文字与本地节点文字分别保存，不能混作同一种来源。

## 结果如何阅读

`alignment.json` 和 `alignment.csv` 都包括 `kg_node_id`、`ontology_node_id`、`relation_type`、`evidence`、`decision_status`，以及便于查看的实体名称、类型。

| `decision_status` | 含义 |
| --- | --- |
| `ALIGNED` | 通过当前自动检查，可查看具体 `MAPS_TO` 或 `INSTANCE_OF` 提案；仍不是人工专家金标准。 |
| `UNMATCHED` | 本轮检索与判断没有可靠候选，不代表整个 Ontology 中必然不存在对应概念。 |
| `REVIEW_REQUIRED` | 证据不足、候选冲突、候选截断、类别层级问题，或者服务/响应失败；保留全部原因等待复核。 |

`relation_type=MAPS_TO` 表示两边在类型、范围、具体程度上指向同一概念。`INSTANCE_OF` 要求 KG 节点有明确的个体身份依据，并且成员归属得到支持；“存在于数据库里”不能证明它是个体。缺少身份元数据时加载器保留 `unknown`，不会仅凭 LLM 猜测接受 `INSTANCE_OF`。

如果 KG 节点本身是类别，且比 Ontology 候选更具体，则记录为 `class_inclusion_proposals` 并交给人工复核，不导出错误的 `INSTANCE_OF`。OLS 中同一标准术语可以提供概念身份依据，但仍需核对本地范围；OLS 的 `is_a` 是类与类之间的包含关系，不能证明个体归属。

对于 `UNMATCHED` 和 `REVIEW_REQUIRED`，最终 `ontology_node_id` 为空，`relation_type` 保持 `UNMATCHED` 占位。应先看 `decision_status` 再解释关系；待复核不等于已证实不匹配。模型提出但未被接受的关系仍保留在 `evidence.assessments` 中。

输出目录还包括：

- `summary.json`：成功对齐数量、比例、`MAPS_TO` / `INSTANCE_OF` / `UNMATCHED` / `REVIEW_REQUIRED` 数量、各类型覆盖率。分母是本次选定的 KG 快照，不是全库实体数。
- `manifest.json`：输入签名、配置、提示词摘要、Ontology 来源和文件校验值、KG 快照元数据及是否完成。
- `ontology_snapshot.json`、`kg_snapshot.json`：本次输入和来源信息。Neo4j element ID 只在指定数据库及对应快照中解释，原始应用 ID 保留在来源属性中。
- `records/`：每条 KG 实体的独立判断记录。
- `llm/`、`ols/`、`embeddings/`：实际使用相应服务时产生的调用日志、核查证据和缓存。CSV 的 `evidence` 单元格保存完整 JSON 文本。

## MVP 的边界与检查

检索的前若干候选和相似度阈值可能漏掉真实对应关系。精确名称或别名冲突会保留多候选；精确候选超过数量上限时需要复核。模型只看检索到的候选，不能声称已经验证整个 Ontology。缺少定义、标准 ID 或物种范围也会降低自动对齐率；当前不以新增类别来填补这些空缺。

已有 Ontology 的边可能带有 `edges_are_provisional` 标记，该信息原样保留。自动引用检查只证明引用存在及基本约束通过，语义正确性仍需要真实案例检查和后续独立专家评估。小规模固定样本的覆盖率不能当作全库覆盖率或匹配准确率。

运行独立模块的测试：

```bash
.venv/bin/python -m unittest discover -s entity_ontology/tests -p 'test_alignment*.py'
```

## 2026-11-22 真实数据试运行

以下为改写提示词前的历史 MVP 运行记录，文件没有被新实验覆盖。后续提示词对照实验见
[PROMPT_STUDY.md](PROMPT_STUDY.md)。当前默认版本为 `verified`，只更新对齐判断提示词，
保留原来的候选、模型配置、引用校验和准入规则。18 个未见数据请求首次全部通过格式及引用校验；
这不代表 18 个实体都完成了对齐，也不代表语义准确率达到 100%。

使用 `liver-kg-core-v02`（3,655 个实体），与现有 370 个 Ontology 概念对齐。
模型为 `qwen3.7-plus`，开启 `text-embedding-v4` 和 OLS。主样本每种类型按 ID 取 5 条；
另取 9 条容易混淆的开发样本。两组有 2 条重叠，共 42 个不同的真实 KG 实体。
这些都是开发阶段检查，不能代表全库覆盖率或泛化准确率。

| 样本 | 数量 | MAPS_TO | INSTANCE_OF | UNMATCHED | REVIEW_REQUIRED |
| --- | ---: | ---: | ---: | ---: | ---: |
| 七类主样本 | 35 | 1（2.86%） | 0 | 22 | 12 |
| 易混淆开发样本 | 9 | 0 | 0 | 1 | 8 |

主样本 Disease 的覆盖率为 1/5（20%），其他类型均为 0/5。各类型的完整数量在
`runs/alignment/pilot-35-plus/summary.json`，映射与证据在同目录的 `alignment.json` 和
`alignment.csv`；开发样本结果在 `runs/alignment/dev-9-plus/`。这些运行文件保留在本地，
没有加入 Git，也没有写入 Neo4j。主样本按类型分组并发运行，再校验并汇总为 35 条唯一结果；
复现脚本为 `runs/alignment/run_pilot_35_plus.py`。

唯一接受的提案为 `type 2 diabetes → Type II diabetes`：两个端点分别核查到
`MONDO:0005148`。KG 缺少定义，主要依据是官方术语及同义词；Ontology 原定义也存在措辞
不够准确的问题，因此这不是专家金标准。开发检查中，FAS 死亡受体与脂肪酸合成酶的同名
候选被拒绝。主样本 DCN、CELSR3 的类别包含建议单独待审，没有变成 `INSTANCE_OF`。

当前限制必须结合失败类型解释：主样本 12 条待审中，9 条是模型格式或引用契约失败，
3 条是语义证据不足；开发样本的 8 条待审中，6 条是契约失败。一次纠正仍不成功时保留
待审，不能把这些记录当成已经完成语义核查。真实 `INSTANCE_OF` 正例尚未验证。
原 Flash 配置的失败日志另存为 `pilot-35-v1`、`dev-9-v1`，没有混入上述 Plus 统计。

全部 88 项代码测试通过（其中 52 项针对新模块）。JSON/CSV 行数、实体唯一性、证据 JSON
和模型日志引用已经核对。原 Ontology 文件 SHA-256 不变；Neo4j 节点数 3,655、关系数
38,246 在只读检查前后一致。代码本身仅执行固定读取查询，不包含图写入功能。
