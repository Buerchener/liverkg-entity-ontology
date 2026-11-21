# LiverKG 实体本体

从 PubMed 标题和摘要抽取肝病实体，合并同义名称，再按「哪一类属于哪一类」构建实体本体。工作台可以查看候选父子关系、OLS 查证结果和论文原句。

**抽取与补漏 → 概念化 → 去重 → 排序 → 建树 → 网页查看**

## 文件导览

| 步骤 | 主要文件 | 文件职责 |
|---|---|---|
| 运行全流程 | [pipeline.py](entity_ontology/pipeline.py) | 从论文输入依次运行各阶段，保存结果和批次清单。 |
| 抽取实体 | [native.py](entity_ontology/extraction/native.py)、[coverage.py](entity_ontology/extraction/coverage.py)、[mentions.py](entity_ontology/extraction/mentions.py) | 调用抽取器、补查漏掉的实体，记录每次提及对应的原句和位置。 |
| 概念化与去重 | [normalization.py](entity_ontology/concepts/normalization.py)、[retrieve_pairs.py](entity_ontology/concepts/retrieve_pairs.py)、[deduplicate.py](entity_ontology/concepts/deduplicate.py) | 整理概念的名称、定义和类型，找出可能同义的概念对，核对后再合并。 |
| 排序与构树 | [order_entities.py](entity_ontology/hierarchy/order_entities.py)、[placement.py](entity_ontology/hierarchy/placement.py)、[entity_placement_dag.py](entity_ontology/hierarchy/entity_placement_dag.py) | 按类型由宽到窄排序，逐个放入层级，校验父子边。 |
| OLS 查证 | [ols_reference.py](entity_ontology/hierarchy/ols_reference.py)、[ontology_ols.py](entity_ontology/hierarchy/ontology_ols.py)、[ols_client.py](entity_ontology/hierarchy/ols_client.py)、[entity_reference.py](entity_ontology/hierarchy/entity_reference.py) | 选择查询动作，统一管理 GO、MONDO 等来源，发出 OLS MCP 请求，保存并筛选查证证据。 |
| 网页复核 | [server.py](entity_ontology/workbench/server.py)、[web/](entity_ontology/workbench/web/) | 读取已完成的批次，展示实体、原句和父子关系。 |

[legacy/](entity_ontology/legacy/) 保留旧的独立 GO Agent 和旧构树调用的兼容代码。单独运行旧工具可用 `python -m entity_ontology.legacy.go_reference`。

## 1. 安装

在仓库根目录运行。支持 macOS 和 Linux，使用 Python 3.12 或 3.13。

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r entity_ontology/requirements.txt
python -m entity_ontology setup-extractor
cp entity_ontology/config.example.json entity-ontology.local.json
```

填入模型服务地址。运行 `read` 时输入密钥并按回车。

```bash
export LIVERKG_API_BASE="你的 OpenAI 兼容服务地址"
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY
```

默认使用同一服务的 Qwen 和 `text-embedding-v4`。模型可在 `entity-ontology.local.json` 修改。

## 2. 输入论文并运行

把论文存成 `inputs/articles.jsonl`。每行一篇，包含唯一的数字 PMID、标题和摘要。

```jsonl
{"pmid":"1","title":"Example title","abstract":"Example abstract."}
```

```bash
python -m entity_ontology run --articles inputs/articles.jsonl --run-id first-batch
```

候选关系经模型复审仍拿不准时，**OLS Reference Agent** 从 GO、MONDO、PRO、ChEBI、Uberon、CL、HGNC 中选择来源，每个概念最多五步。搜索优先用 OLS MCP，正式条目和直接父类通过 OLS REST 核对。程序核对两个概念对应的条目及关系方向，再把符合条件的证据交给模型复审。HGNC 只用于核对人类基因身份。每步查询及结果都会留档。

可以用 `--no-entity-reference` 关闭全部外部查证，或用 `--no-go-reference` 只关闭 GO。用新批次名运行才能为已完成的文献批次生成新版结果。

## 3. 看结果

```bash
python -m entity_ontology serve --open
```

网页默认打开最近完成的批次。结果保存在 `runs/entity_ontology/first-batch/`，包括原句、概念清单和候选父子关系。同一输入、配置和批次名可以续跑。

在总览里可以从论文原句一路点到去重后的概念和候选父类。已有批次在其他目录时，用 `python -m entity_ontology serve --runs-dir <批次父目录>` 打开。

同一批 100 篇论文的构树结果可以在 GitHub Releases 下载。[统一 OLS 版](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) 包含候选父子关系和对照报告，[旧版](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-reference-v2) 可用于对照。

抽取沿用 [LiverKG 固定版本](https://github.com/Buerchener/FYP-liver-disease/commit/fc9ebd72971318a7738f15fc2b86369a552656c2)。建树允许一个概念有多个父类。抽取及建树结果均保存在本地文件，供人工复核。
