# LiverKG 实体本体

从 PubMed 标题和摘要里找出肝病相关实体，合并同义名称，再按「哪一类属于哪一类」组织概念。网页可以查看每个概念的原句和它在层级里的位置。

**抽取与补漏 → 概念化 → 去重 → 排序 → 建树 → 网页查看**

## 代码在哪

| 步骤 | 目录 | 先看哪些文件 |
|---|---|---|
| 1. 从论文找实体 | [extraction/](entity_ontology/extraction/) | `native.py` 调用同学的抽取器，`coverage.py` 查漏，`mentions.py` 整理原句和位置。 |
| 2. 把提及变成概念 | [concepts/](entity_ontology/concepts/) | `normalization.py` 判断词指什么，`retrieve_pairs.py` 找相似候选，`deduplicate.py` 判断同义并合并。 |
| 3. 给概念找位置 | [hierarchy/](entity_ontology/hierarchy/) | `order_entities.py` 由宽到窄排序，`placement.py` 逐个建树，`entity_reference.py` 通过 OLS 查证。 |
| 4. 网页查看和复核 | [workbench/](entity_ontology/workbench/) | `server.py` 读取批次结果，`web/` 展示实体、原句和父子关系。 |

想看四步怎么接起来，打开 [pipeline.py](entity_ontology/pipeline.py)。

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

建树复审仍拿不准时，**OLS Reference Agent** 读取实体类型和待验证的父子或同义问题，在 GO、MONDO、PRO、ChEBI、Uberon、CL 和 HGNC 中选择来源。每看一次结果，它都可以换词、换来源、核对任一端的正式记录或结束，每个概念最多五次查询动作。搜索优先用 OLS MCP，正式条目与直接父类通过 OLS REST 核对。只有在同一来源里找到两端及方向一致的直接父子关系，才把这条关系交给模型复审。HGNC 只用于核对人类基因身份。查证记录保留每一步选择及其结果。

可以用 `--no-entity-reference` 关闭全部外部查证，或用 `--no-go-reference` 只关闭 GO。用新批次名运行才能为已完成的文献批次生成新版结果。

## 3. 看结果

```bash
python -m entity_ontology serve --open
```

网页默认打开最近完成的批次。结果保存在 `runs/entity_ontology/first-batch/`，包括原句、概念清单和候选父子关系。同一输入、配置和批次名可以续跑。

在总览里可以从论文原句一路点到去重后的概念和候选父类。已有批次在其他目录时，用 `python -m entity_ontology serve --runs-dir <批次父目录>` 打开。

同一批 100 篇论文的构树结果可以在 GitHub Releases 下载。[统一 OLS 版](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) 包含候选父子关系和对照报告，[旧版](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-reference-v2) 可用于对照。

抽取沿用 [LiverKG 固定版本](https://github.com/Buerchener/FYP-liver-disease/commit/fc9ebd72971318a7738f15fc2b86369a552656c2)。建树允许一个概念有多个父类。抽取及建树结果均保存在本地文件，供人工复核。
