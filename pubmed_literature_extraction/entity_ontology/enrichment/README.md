# KG-Guided Ontology Enrichment

从已完成 Alignment 的 LiverKG 快照中，抽取此前没有证据连接的真实记录。扩充结果和映射写入新的本地文件。流程只使用冻结输入，无需 Neo4j 连接。

1. 合并现有 Alignment 的身份映射和有证据的分类连接。按类型用固定种子抽样，保留原始 ID、属性、证据和文件位置。
2. 保留已有定义。缺失时从 NCBI Gene、HGNC、Reactome、KEGG、本地 OBO 或 OLS 查找定义。在提供 PubMed 摘要时保存原文提及。只有原文或权威记录支持的定义才能进入插入流程。
3. 复用 Alignment 在现有图中查同义概念。确认为已存在时输出 `MAPS_TO`。无可靠同义候选时，进入 `place_one()`，在副本上调用 `EntityDAG.place()`。
4. 逐条独立验证新增边，检查类型、环、严格的上下位关系和已否定关系是否会被传递路径重新引入。类型根使用单独的类型归属审核。生成中间父类需要 OLS 支持的同范围术语、权威定义和有方向的严格关系。
5. 对拟插入节点重新跑 Alignment。根据原始 Neo4j database 和 element ID、标准 ID、类型、物种、名称及定义核对目标。未通过时丢弃整次尝试，包括新增的父类和边。

## 运行真实样本

以下命令从本仓库的 `pubmed_literature_extraction/` 目录运行。按主 README 安装 ontology 依赖并启用虚拟环境。程序通过环境变量读取模型凭据。

105 节点实验使用签名过的 KG 快照、原对齐和文献本体。下例中的 `runs/` 路径代表这些本地输入的示例位置。复现历史实验还需要对应版本的冻结输入。在没有这些文件时仍可运行离线单元测试，或查阅[实验报告与复核结果](../../benchmark_output/ontology-enrichment-evidence-v4/report.zh.md)。

```sh
export LIVERKG_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY

python -m entity_ontology.enrichment \
  --ontology runs/entity_ontology/entity-100-ols-agent-v1/incremental-placement/placement.json \
  --ontology-grounding runs/alignment/disease-protein-grounding/ontology-enriched.json \
  --kg-snapshot runs/alignment/prompt-study-v1/heldout-sealed/pool_snapshot.json \
  --alignment runs/alignment/full-3655-grounded/alignment.json \
  --alignment runs/alignment/full-3655-grounded/categories/categories.json \
  --alignment runs/alignment/no-candidate-embedding-audit/results.json \
  --cell-ontology runs/alignment/full-3655-grounded/categories/cl.obo \
  --articles /absolute/path/to/pubmed_converted_1927.jsonl \
  --config entity_ontology/enrichment/config.flash.example.json \
  --sample-size 42 --seed 20261122 --max-llm-calls 420 \
  --output runs/enrichment/my-pilot
```

`--alignment` 接受多个来源，须合计覆盖同一份 KG 快照。可选的 `--ontology-grounding` 只为检索补充标准 ID。可用 `--uberon` 提供本地 Uberon，或用 `--no-ols` 关闭 OLS。模型调用共享上限，已完成的调用保存到 `llm/`。运行中断后，使用相同参数和输出目录可继续利用缓存。

## 结果文件

| 文件 | 内容 |
| --- | --- |
| `manifest.json`、`sample.json` | 输入文件校验值、抽样规则、原始样本与基线证据 |
| `ontology-enriched.json` | 独立本体快照，保留原节点和边。每条新增边记录审核和来源 |
| `alignment-enriched.json` | 历史连接与本次新发现的 `MAPS_TO` |
| `records.json`、`records/` | 每条样本的判定、待审原因、源记录、层级与重对齐追踪 |
| `alignment/`、`llm/`、`ols/`、`sources/` | Alignment 原始输出、模型请求、OLS 工具结果和定义证据 |
| `summary.json` | 结果和覆盖率。分别统计有证据连接和同一概念映射 |

全库覆盖率按冻结基线加本批新连接计算。分类型均衡样本可用于开发复核，不能外推到所有未匹配节点。类型根归属数和严格的生物医学子类关系数分别计入 `new_edge_support`。

```sh
python -m unittest discover -s entity_ontology/tests -p 'test_enrichment*.py'
```

## 父类证据回归实验

可以复现同一批失败记录，也可以单独运行预先留出的新样本。先从完成的 42 条试验冻结两个清单。留出组使用固定种子，排除全部 42 条和已有证据连接的节点。

```sh
python -m entity_ontology.enrichment.experiment freeze \
  --pilot runs/enrichment/pilot-42-verified --directory runs/enrichment/evidence-v2
```

两次 Flash 实验都使用上文相同的文献本体和输入文件。模型配置使用 runs/enrichment/evidence-v2/model-config-flash.json。将运行参数中的抽样选项改为 `--cohort runs/enrichment/evidence-v2/replay-12.json`，输出到 `runs/enrichment/evidence-v2/replay-flash`。第二次使用 `unseen-12.json` 和独立的 `unseen-flash` 目录。两个运行分别从原始本体开始。代码、配置和输入的校验值可在各自的 `manifest.json` 中核对。

```sh
python -m entity_ontology.enrichment.experiment aggregate \
  --directory runs/enrichment/evidence-v2 \
  --replay-run runs/enrichment/evidence-v2/replay-flash \
  --unseen-run runs/enrichment/evidence-v2/unseen-flash
```

逐条结果在 `records/`。从 `kg.reference_terms` 可追溯已核验的外部身份与直接父类。`placement.typed_relation_reviews` 保存对原候选的六类判定。改查其他父类时，`placement.parent_fallback` 保留每一跳、停止原因、候选的范围判断以及最终选用的父类。先查直接父类，最深 3 跳，每个节点最多读取 12 个候选父类记录。Reactome 的通路包含关系、GO 注释、UniProt 的复合物信息和 MeSH 的宽泛主题词分别保存原始谓词。最终结果将根节点归类与具体的生物医学上下位关系分别计数。

## 复核运行结果

在 `pubmed_literature_extraction/` 下，以下两步使用某一次完整本地运行。先逐条核查权威本体断言，再依照审查决定生成单独的发布副本。

```sh
python scripts/enrichment/check_assertions.py "$RUN" --cache "$OLS_CACHE" --inputs-dir "$FROZEN_INPUTS" --output "$AUDIT_JSON"
python scripts/enrichment/release_review.py "$RUN" "$DECISIONS_JSON" "$REVIEWED_DIR" --ontology "$BASE_ONTOLOGY"
```

`RUN` 是运行日志和原始记录所在目录，`DECISIONS_JSON` 使用该次运行的节点 ID。`--inputs-dir` 与 `--ontology` 可用于迁移后的输入文件，脚本会核对清单中的校验值。公开实验包提供可直接查看的结果和来源，逐例记录使用公开案例编号。
