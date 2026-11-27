# LiverTrail v3 骨干验收

2026-11-28。本机 Neo4j Community 5.26.34，从归档资源离线构建并通过重复导入验证。

数据库为 neo4j，Bolt 地址为 bolt://127.0.0.1:17687。浏览器由本机 Neo4j 提供。 [打开 Neo4j Browser](http://127.0.0.1:17474/browser/)

## 规模与证据

| 指标 | v2 | v3 | 增加 |
| --- | --- | --- | --- |
| 独立节点 | 3546 | 4784 | 1238 |
| 关系 | 38203 | 46812 | 8609 |
| 代谢物 | 36 | 82 | 46 |
| 通路及分类 | 1721 | 2265 | 544 |

Stage 是五个 Disease 节点的附加标签，独立节点按 Neo4j 节点去重。

### 按节点标签

| 标签 | 节点 |
| --- | --- |
| BackboneClass | 48 |
| CellType | 154 |
| Disease | 5 |
| ExternalReference | 600 |
| Gene | 836 |
| Metabolite | 82 |
| Pathway | 2265 |
| Protein | 793 |
| Stage | 5 |
| Tissue | 1 |

### 按关系类型

| 类型 | 关系 |
| --- | --- |
| ASSOCIATED_WITH | 1036 |
| ASSOCIATED_WITH_METABOLITE | 115 |
| CROSS_REFERENCE | 713 |
| ENCODES | 793 |
| EXPRESSED_IN | 17745 |
| INTERACTS_WITH | 7154 |
| IS_A | 4265 |
| METABOLITE_ASSOCIATED_WITH_DISEASE | 48 |
| PARTICIPATES_IN | 10288 |
| PROGNOSTIC_IN | 1384 |
| PROGRESSES_TO | 4 |
| STAGE_DIFFERENTIAL | 1002 |
| SUBPATHWAY_OF | 2265 |

### 按关系来源与证据等级

| 来源 | 关系 |
| --- | --- |
| DisGeNET | 1036 |
| HMDB | 338 |
| Human Protein Atlas | 19129 |
| KEGG | 5924 |
| LiverTrail | 5149 |
| Reactome | 7289 |
| STRING | 7154 |
| STRING_mapping | 793 |

| 证据等级 | 关系 |
| --- | --- |
| contextual_background | 43390 |
| direct_disease_evidence | 2420 |
| inferred_or_hypothesis | 1002 |

## 通路、代谢物与阶段

HMDB 5.0 解压流共核验 217,920 条记录，保留 36 个 v2 代谢物。
纳入新增代谢物时逐条记录化学分类和通路匹配依据。 [筛选规则](../docs/backbone_v3_schema.md)

| 化学家族 | v2 | 新增 | v3 |
| --- | --- | --- | --- |
| bile_acid | 5 | 31 | 36 |
| ceramide | 0 | 10 | 10 |
| fatty_acid | 2 | 5 | 7 |
| other | 29 | 0 | 29 |

| HMDB 化学类 | 代谢物 |
| --- | --- |
| Steroids and steroid derivatives | 36 |
| Organooxygen compounds | 14 |
| Sphingolipids | 10 |
| Fatty Acyls | 8 |
| Glycerophospholipids | 4 |
| Carboxylic acids and derivatives | 2 |
| Phenols | 2 |
| Benzene and substituted derivatives | 1 |
| Organic carbonic acids and derivatives | 1 |
| Organonitrogen compounds | 1 |
| Prenol lipids | 1 |
| Pyridines and derivatives | 1 |
| Thioethers | 1 |

通路层级最大深度为 11 跳。归档覆盖 1,719 个 v2 通路。其余两个 Reactome ID 保留原有节点和成员关系，等待可核验的层级记录。

| 通路 ID | 顶层分类 | 来源 | 子树通路与分类数 | 最大深度 |
| --- | --- | --- | --- | --- |
| KEGG-BRITE:br08901:6e720fd4075e77e1bf3e | Human Diseases | KEGG | 108 | 2 |
| KEGG-BRITE:br08901:863f56aae2a999023670 | Organismal Systems | KEGG | 95 | 2 |
| KEGG-BRITE:br08901:357239d73187d3ec2ebc | Metabolism | KEGG | 76 | 2 |
| KEGG-BRITE:br08901:f4193818b44237514e8c | Environmental Information Processing | KEGG | 41 | 2 |
| KEGG-BRITE:br08901:9b112cb4c7291cc47a1b | Genetic Information Processing | KEGG | 35 | 2 |
| KEGG-BRITE:br08901:ba28280a6265ec28604e | Cellular Processes | KEGG | 30 | 2 |
| Reactome:R-HSA-1643685 | Disease | Reactome | 348 | 9 |
| Reactome:R-HSA-162582 | Signal Transduction | Reactome | 336 | 11 |
| Reactome:R-HSA-168256 | Immune System | Reactome | 189 | 8 |
| Reactome:R-HSA-1430728 | Metabolism | Reactome | 180 | 5 |
| Reactome:R-HSA-1640170 | Cell Cycle | Reactome | 114 | 7 |
| Reactome:R-HSA-74160 | Gene expression (Transcription) | Reactome | 112 | 6 |
| Reactome:R-HSA-1266738 | Developmental Biology | Reactome | 94 | 5 |
| Reactome:R-HSA-392499 | Metabolism of proteins | Reactome | 75 | 5 |
| Reactome:R-HSA-8953897 | Cellular responses to stimuli | Reactome | 56 | 5 |
| Reactome:R-HSA-73894 | DNA Repair | Reactome | 55 | 6 |
| Reactome:R-HSA-382551 | Transport of small molecules | Reactome | 46 | 3 |
| Reactome:R-HSA-8953854 | Metabolism of RNA | Reactome | 34 | 3 |
| Reactome:R-HSA-5653656 | Vesicle-mediated transport | Reactome | 32 | 5 |
| Reactome:R-HSA-109582 | Hemostasis | Reactome | 31 | 4 |
| Reactome:R-HSA-112316 | Neuronal System | Reactome | 29 | 6 |
| Reactome:R-HSA-1500931 | Cell-Cell communication | Reactome | 26 | 8 |
| Reactome:R-HSA-5357801 | Programmed Cell Death | Reactome | 26 | 4 |
| Reactome:R-HSA-4839726 | Chromatin organization | Reactome | 20 | 5 |
| Reactome:R-HSA-1474244 | Extracellular matrix organization | Reactome | 19 | 3 |
| Reactome:R-HSA-69306 | DNA Replication | Reactome | 13 | 3 |
| Reactome:R-HSA-9748784 | Drug ADME | Reactome | 11 | 2 |
| Reactome:R-HSA-1852241 | Organelle biogenesis and maintenance | Reactome | 10 | 4 |
| Reactome:R-HSA-9612973 | Autophagy | Reactome | 10 | 4 |
| Reactome:R-HSA-9709957 | Sensory Perception | Reactome | 10 | 3 |
| Reactome:R-HSA-1474165 | Reproduction | Reactome | 7 | 2 |
| Reactome:R-HSA-9909396 | Circadian clock | Reactome | 7 | 1 |
| Reactome:R-HSA-397014 | Muscle contraction | Reactome | 6 | 2 |
| Reactome:R-HSA-9609507 | Protein localization | Reactome | 4 | 2 |
| Reactome:R-HSA-8963743 | Digestion and absorption | Reactome | 3 | 2 |

| 阶段 | 疾病 | 基因 |
| --- | --- | --- |
| 1 | NAFLD | 103 |
| 2 | NASH | 107 |
| 3 | Fibrosis | 2 |
| 4 | Cirrhosis | 169 |
| 5 | HCC | 655 |

| 阶段模式 | 基因 |
| --- | --- |
| early_only | 63 |
| late_only | 727 |
| transition | 46 |

| 相邻阶段 | 变化 | 基因 |
| --- | --- | --- |
| 1 → 2 | added | 4 |
| 2 → 3 | added | 2 |
| 2 → 3 | removed | 107 |
| 3 → 4 | added | 169 |
| 3 → 4 | removed | 2 |
| 4 → 5 | added | 602 |
| 4 → 5 | removed | 116 |

## 重跑和文献入库

清洁实例首次写入 4,784 个节点与 46,812 条关系。核验了 22 项唯一约束。重复应用同一快照的节点新增、关系新增、属性更新均为 0。全库骨干节点和边的来源、版本及更新时间完整。

使用打桩的 B 输出验证文献接入。专用谓词同时写入文献概念与真实骨干端点，并保留 stage_order 和 Evidence 原句位置。缺失骨干端点会回滚整批。两批共享 PMID 时，撤回一批保留另一批的文章。测试数据撤回后，骨干计数与初始导入一致。

工作台已支持 v3 对齐和四档 novelty。三模型配置通过打桩的九阶段流水线测试，三路模型与 v3 快照均记录在批次清单。

### 真实语料运行状态

原 500 篇和新增 1,464 篇共有 1,964 个唯一 PMID。全量队列保留全部原始标题和摘要。

| 模型角色 | 模型 | 五篇试点 HTTP 成功调用 | 输入 token | 输出 token |
| --- | --- | ---: | ---: | ---: |
| 主模型 | deepseek-v4.1-flash | 99 | 112,592 | 28,444 |
| LangExtract | [福利]gemini-3.8-flash | 8 | 25,962 | 5,459 |
| 向量 | qwen3.7-text-embedding | 3 | 2,001 | 0 |

五篇完成全部九个阶段，失败篇目为 0。DeepSeek 在增量放置时出现一次 HTTP 429，降速后从检查点恢复。两次有效运行分别用时 249.1 秒和 314.7 秒，合计 563.8 秒。

下表为各篇 B 抽取阶段的调用次数和 token。后续 C 阶段的跨文献调用计入上表合计。

| PMID | 耗时（秒） | 实体 | 候选关系 | DeepSeek 调用 / token | Gemini 调用 / token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 41354212 | 30.07 | 3 | 4 | 4 / 8,970 | 1 / 4,044 |
| 41518251 | 20.39 | 3 | 1 | 2 / 2,937 | 1 / 3,208 |
| 41948344 | 38.97 | 19 | 13 | 4 / 7,140 | 2 / 9,218 |
| 41668355 | 35.07 | 13 | 5 | 4 / 9,468 | 2 / 7,913 |
| 41665498 | 13.30 | 6 | 0 | 0 / 0 | 2 / 7,038 |

试点投影出 7 条 AUTO、7 条 REVIEW 和 1 条 BLOCKED。7 条 AUTO 的原句和字符位置全部核验，证据均出自 PMID 41948344。本机 Neo4j 现查到 38 个概念、13 条 SAME_AS、7 条 AUTO 概念关系和 7 个证据节点。其中 3 条文献关系也写在骨干节点之间，5 条概念关系可从骨干沿对齐和文献关系追溯到原句。

北京时间 2026-11-28 06:06 启动全量。已入库的 5 篇直接复用，余下 1,959 篇分 70 批，两个批次并行，每批最多 4 个抽取 worker。DeepSeek 分别走百炼工作区和 InferAI，Gemini 走 Gemai，Qwen 通过百炼生成 1,024 维向量。根据试点耗时和限流情况，初估还需 28–40 小时，预计 11-29 10:06 至 11-29 22:06 完成。

按试点用量线性估算，全量约需 5,540 万 DeepSeek token、1,234 万 Gemini token 和 79 万 Qwen 输入 token。批次规模、复用、重试都会影响实际用量。费用按服务账单核算。

本机进度文件为 ~/.local/share/livertrail/v3-full-1964/status.json。作业结束时将各批的图谱 JSONL 和失败 PMID 汇总至 ~/.local/share/livertrail/v3-full-1964/snapshot-index.json。

## 离线测试

| 套件 | 通过 | 跳过 | 缺 BioRED 数据 |
| --- | --- | --- | --- |
| ontology | 53 | 0 | 0 |
| extraction | 432 | 3 | 13 |

## 查询与快照

快照 ID 为 b8f4cb508237f1f8a1a4d31ddba0ff9f612b4ab0ff4676883b90f60e0e7383b6。本机 JSONL 位于 data/backbone_v3，归档来源及 SHA-256 位于 data/backbone_v3_sources/manifest.json。

以下查询在本机 v3 运行。完整查询和结果随 JSON 报告保存。 [详细验收数据](backbone_v3_summary.json)

### 计数

~~~cypher
MATCH (n {backbone_version: 'v3'}) WITH count(n) AS nodes MATCH ()-[r {backbone_version: 'v3'}]->() RETURN nodes, count(r) AS relationships
~~~

~~~json
[
  {
    "nodes": 4784,
    "relationships": 46812
  }
]
~~~

### 通路深度

~~~cypher
MATCH p=(:Pathway)-[:SUBPATHWAY_OF*0..20]->(root:Pathway)
    WHERE NOT (root)-[:SUBPATHWAY_OF]->() RETURN max(length(p)) AS depth
~~~

~~~json
[
  {
    "depth": 11
  }
]
~~~

### HMDB 专用关系

~~~cypher
MATCH (:Metabolite)-[r:METABOLITE_ASSOCIATED_WITH_DISEASE {backbone_version:'v3'}]->(:Stage)
    RETURN r.source AS source, r.evidence_class AS evidence_class, count(r) AS count
~~~

~~~json
[
  {
    "source": "HMDB",
    "evidence_class": "contextual_background",
    "count": 48
  }
]
~~~

### 阶段基因

~~~cypher
MATCH (s:Stage {backbone_version:'v3'})
    OPTIONAL MATCH (g:Gene)-[:ASSOCIATED_WITH {backbone_version:'v3'}]->(s)
    RETURN s.stage_order AS stage, s.name AS name, count(DISTINCT g) AS genes ORDER BY stage
~~~

~~~json
[
  {
    "stage": 1,
    "name": "NAFLD",
    "genes": 103
  },
  {
    "stage": 2,
    "name": "NASH",
    "genes": 107
  },
  {
    "stage": 3,
    "name": "Fibrosis",
    "genes": 2
  },
  {
    "stage": 4,
    "name": "Cirrhosis",
    "genes": 169
  },
  {
    "stage": 5,
    "name": "HCC",
    "genes": 655
  }
]
~~~
