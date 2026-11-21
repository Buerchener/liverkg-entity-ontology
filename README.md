# LiverKG Entity Ontology

从 PubMed 肝病文献中识别实体，归并指向同一概念的名称，并构建可回到论文原句查证的候选分类层级。输出是按实体类型组织的有向无环图（DAG）。每条 `is_a` 边由子类指向父类，一个概念可以有多个直接父类。

*Research software for evidence-linked ontology construction. The 100-article release includes a candidate hierarchy, placement decisions and a comparison of two external reference strategies.*

[查看 100 篇论文的公开结果](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) · [运行单篇示例](#运行单篇示例) · [查看方法与代码](#方法与代码)

## 从一句原文到一条候选边

在 PMID `41692141` 的摘要中，作者报告了包括 **collagen I** 在内的几种标志物的表达变化。在公开的 100 篇批次中，可以沿同一条记录查看三层信息。[论文摘要](https://pubmed.ncbi.nlm.nih.gov/41692141/) · [该批次的图与放置记录](https://github.com/Buerchener/liverkg-entity-ontology/releases/download/results-entity-100-ols-agent-v1/placement.json)

| 层次 | 示例中的记录 | 如何解读 |
| --- | --- | --- |
| 文献提及 | `41692141:M05` · collagen I | 定位到这篇摘要中的一次具体出现。 |
| 实体概念 | `C-ad58a8434684` · collagen I · Protein | 给本体中的概念一个标识，并保留其来源提及。 |
| 候选父子边 | `collagen I → extracellular matrix proteins` | 这条分类关系来自构树阶段，记录的支持方式是模型判断与复审（`dual_model_review`）。原句提供的是实体出现的证据。 |

## 方法与代码

本体覆盖疾病、基因、蛋白质、代谢物、通路、组织和细胞类型这七类实体。流程由 [pipeline.py](entity_ontology/pipeline.py) 串联，各阶段的产物保存在同一批次目录中。

| 阶段 | 处理方式 | 主要产物 |
| --- | --- | --- |
| [实体抽取](entity_ontology/extraction/native.py)与[补漏](entity_ontology/extraction/coverage.py) | 调用固定版本的 LiverKG 抽取器，将提及链接到 PMID、句子和字符位置。补查测量、表达等语境中的漏词，校验新增提及的原文位置。 | `input_mentions.json` |
| [概念化](entity_ontology/concepts/normalization.py) | 根据原句生成名称、类型、定义和别名，再独立复核类型。记录保留、超出范围和待审状态。 | `normalization.json` |
| [候选检索](entity_ontology/concepts/retrieve_pairs.py) | 对概念描述做向量化。候选对来自余弦 KNN（默认每条取 2 个近邻）以及名称或别名匹配。HDBSCAN 供工作台浏览聚类。 | `retrieval/retrieved_pairs.json` |
| [等价复核与去重](entity_ontology/concepts/deduplicate.py) | 模型核对两个名称是否指向同一粒度的实体。跨类型但同名或共享别名时，先回到原句复核类型。组内只差一对未判断时，单独补查。每一对均确认等价后才合并。 | `equivalence.json` |
| [概念排序](entity_ontology/hierarchy/order_entities.py) | 在每种实体类型内从宽到窄排列，确定逐个放入图中的顺序。 | `concept_order.json` |
| [增量构树](entity_ontology/hierarchy/placement.py) | 提议父子关系，独立复核包含方向，再检查同类型、无环和根节点可达等约束。记录已放置和待审概念。 | `incremental-placement/placement.json` |

### OLS Reference Agent

构树复审仍有疑问时，agent 根据实体类型和待验证的问题选择 GO、MONDO、PRO、ChEBI、Uberon、CL 或 HGNC。每个目标概念最多有五个决策轮次。模型选择搜索、查看词条、核实概念或停止。Python 执行查询，把整理后的结果交给下一轮。词条搜索优先使用 OLS MCP，搜索失败时回退到 REST。正式词条和直接父类通过 REST 核对。[决策循环](entity_ontology/hierarchy/ols_reference.py) · [OLS 查询](entity_ontology/hierarchy/ontology_ols.py) · [MCP 客户端](entity_ontology/hierarchy/ols_client.py)

要用外部证据支持严格的父子关系，两个本地概念须分别对应同一来源中的明确词条，并查到从子类到父类的直接 `is_a` 边。两个名称指向同一外部标识时，可用于身份核对。符合条件的证据会交回构树模型复审。[证据筛选规则](entity_ontology/hierarchy/entity_reference.py)

## 已发布的 100 篇结果

统一 OLS 版沿用基线批次的 **100 篇论文和 545 个已排序概念**，复用了匹配的放置提议和常规模型复审记录，重新运行 OLS 查证和带证据的复审。下表对应已发布的构树版本。主分支的跨类型别名复核与组内补查是在此后加入的。[统一 OLS 版及对照报告](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) · [基线版本](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-reference-v2)

| 统一 OLS 版的观察项 | 结果 |
| --- | ---: |
| 实际入图的文献概念（另有 7 个类型根节点） | 370 |
| 具体概念之间的候选父子边（另有 240 条连向类型根节点） | 171 |
| 触发外部查证的目标概念 | 58 |
| 记录的 OLS 查询动作（39 次 MCP，125 次 REST） | 164 |

两版最终保留的具体父子边完全相同。统一版为其中 0 条找到了外部本体明确记录的直接 `is_a` 边。

发布页提供图文件、两版对照报告和 SHA-256 校验值。下载 `placement.json` 即可分析公开图。查看原句、聚类和逐步决策的完整工作台需要本地的完整批次目录。

## 运行单篇示例

在 macOS 或 Linux 上安装 Git 和 Python 3.12 或 3.13。完整流程需要可同时访问对话模型与向量模型的 OpenAI 兼容服务。示例配置使用 `qwen3.7-flash` 和 `text-embedding-v4`。抽取以 dry-run 模式运行，结果写入本地文件。[模型配置](entity_ontology/config.example.json) · [固定版本的上游抽取器](https://github.com/Buerchener/FYP-liver-disease/commit/fc9ebd72971318a7738f15fc2b86369a552656c2)

**1. 安装并配置模型服务。** 将下面的服务地址替换为自己的兼容接口地址。执行 `read` 后，输入 API key 并按回车。

```bash
git clone https://github.com/Buerchener/liverkg-entity-ontology.git
cd liverkg-entity-ontology
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r entity_ontology/requirements.txt
python -m entity_ontology setup-extractor
cp entity_ontology/config.example.json entity-ontology.local.json

export LIVERKG_API_BASE="https://<provider-host>/v1"
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY
```

**2. 准备一篇论文并运行。** 下面的命令从上一步下载的固定版本语料中取出开头示例的论文。使用自己的文献时，按 JSONL 格式每行放一篇，包含唯一的数字 PMID（`pmid`）、标题（`title`）和摘要（`abstract`）。

```bash
mkdir -p inputs
extractor_dir=.cache/entity_ontology/extractor/fc9ebd72971318a7738f15fc2b86369a552656c2
grep -m 1 '"pmid": "41692141"' \
  "$extractor_dir/pubmed_literature_extraction/extraction_output/pubmed_converted_500.jsonl" \
  > inputs/example.jsonl
python -m entity_ontology run --articles inputs/example.jsonl --run-id collagen-example
```

**3. 打开工作台。** 在本地工作台选择已完成的批次，可从概念回看原句、复核去重判断，或展开多父类层级。同一篇论文单独运行时，可供选择的父类取决于该批次中的概念。

```bash
python -m entity_ontology serve --open
```

默认地址为 `http://127.0.0.1:4320`。本例的产物保存在 `runs/entity_ontology/collagen-example/`。可用 `serve --runs-dir <批次父目录>` 查看其他位置的完整批次。[工作台入口](entity_ontology/workbench/server.py)

## 复现与测试

- **续跑**时，用相同输入、模型配置和批次名重新执行 `run`。批次清单（`manifest.json`）记录输入与配置摘要、阶段状态和完成计数。比较新策略时使用新的 `--run-id`，保留原始批次供对照。
- **做对照实验**时，可用 `--knn-k` 调整检索近邻数，`--no-coverage` 关闭抽取补漏，`--no-entity-reference` 关闭全部外部查证，`--no-go-reference` 仅停用 GO 来源。
- 在仓库根目录运行下列命令可**验证实现**。测试使用合成输入和模拟服务，覆盖去重、OLS 证据筛选和多父类构树。关系准确率需要用独立标注另行评估。

```bash
python -m unittest discover -s entity_ontology/tests
```
