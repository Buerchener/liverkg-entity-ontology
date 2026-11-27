# LiverTrail v3 骨干图谱

## 本机构建

在仓库根目录运行，使用已安装 LiverKG 的 Python 3.12 环境。构建输入为 v2 TSV、归档的通路文件和 HMDB 5.0 压缩 XML。运行时通过进程环境变量 NEO4J_PASSWORD 读取本机数据库认证信息。

~~~bash
liverkg backbone build --version v3 --dry-run --output data/backbone_v3
liverkg backbone build --version v3 --apply --database neo4j \
  --uri bolt://127.0.0.1:17687 --output data/backbone_v3 \
  --report /tmp/livertrail-backbone-v3-build.json
~~~

归档输入默认在 data/backbone_v3_sources。可以用 --sources、--v2-data、--hmdb-xml 指定经过校验的副本。构建逐条读取并校验解压后的 XML，输出 nodes.jsonl、relationships.jsonl 和 manifest.json。manifest.json 内的快照 ID 由两份 JSONL 的 SHA-256 共同确定，入库时写到根节点。数据库中的 updated_at 记录首次写入或数据发生变化的时间。相同快照重复导入保留既有时间戳。单独用快照复现时运行以下命令。

~~~bash
liverkg backbone build --version v3 --from-snapshot data/backbone_v3 \
  --apply --uri bolt://127.0.0.1:17687 --database neo4j
~~~

## 节点键和层级

| 标签 | 主键 | v3 属性 |
| --- | --- | --- |
| Disease、Stage | disease_id | stage_order、aliases、related_aliases、xrefs |
| Gene | gene_id | NCBIGene 与 Ensembl 标识、五阶段证据数、stage_pattern |
| Protein | protein_id | STRING 与可匹配的 Ensembl 蛋白标识 |
| Pathway | pathway_id | Reactome 或 KEGG 标识、GO 注释、成员基因数、阶段画像 |
| Metabolite | metabolite_id | HMDB、ChEBI、KEGG Compound、PubChem CID、化学分类、筛选路径、阶段画像 |
| Tissue | tissue_id | HPA 组织名和别名 |
| CellType | cell_type_id | HPA 细胞类型名和别名 |
| BackboneClass | class_id | 根类、七个类型类和 HMDB 化学分类层级 |
| ExternalReference | xref | 归档的 Reactome 通路明确引用的 GO 标识 |

主键按标签建立唯一约束，关系按类型和标识属性建立唯一约束。每个骨干节点和关系均带 source、source_version、updated_at。来源版本可追溯到归档文件摘要或派生规则版本。Stage 是现有五个 Disease 节点的附加标签。NAFLD/MASLD 和 NASH/MASH 的命名写入可精确匹配的别名。MetALD 放入相关名称，匹配到时交由审核。阶段链表示项目的组织顺序。 [AASLD 命名说明](https://www.aasld.org/new-masld-nomenclature)

~~~mermaid
graph TD
  Gene[Gene] -->|IS_A| GeneClass[Gene 类]
  Disease[Disease / Stage] -->|IS_A| DiseaseClass[Disease 类]
  Metabolite[Metabolite] -->|IS_A| ChemicalClass[HMDB 化学分类链]
  Metabolite -->|METABOLITE_ASSOCIATED_WITH_DISEASE| Disease
  ChemicalClass -->|IS_A| MetaboliteClass[Metabolite 类]
  Gene -->|ASSOCIATED_WITH| Disease
  Gene -->|STAGE_DIFFERENTIAL| Disease
  Gene -->|ASSOCIATED_WITH_METABOLITE| Metabolite
  Gene -->|PARTICIPATES_IN| Pathway[Pathway]
  Metabolite -->|PARTICIPATES_IN| Pathway
  Pathway -->|SUBPATHWAY_OF| ParentPathway[祖先通路]
  Pathway -->|CROSS_REFERENCE| GO[GO 参考标识]
  ParentPathway -->|IS_A| PathwayClass[Pathway 类]
  GeneClass -->|IS_A| Root[LiverTrail Entity]
  DiseaseClass -->|IS_A| Root
  PathwayClass -->|IS_A| Root
  MetaboliteClass -->|IS_A| Root
  OtherClasses[Protein、Tissue、CellType 类] -->|IS_A| Root
  Concept[文献概念] -->|SAME_AS| Metabolite
  Concept -->|IS_A| ChemicalClass
~~~

## 关系与证据

| 类型 | 允许的骨干方向 | 证据口径 |
| --- | --- | --- |
| ASSOCIATED_WITH | Gene → Disease | DisGeNET 直接疾病证据 |
| METABOLITE_ASSOCIATED_WITH_DISEASE | Metabolite → Disease | HMDB 注释为背景证据 |
| PROGNOSTIC_IN | Gene → Disease | HPA TCGA 和验证队列的肝癌生存关联为直接疾病证据，保留队列、方向和 p 值 |
| PROGRESSES_TO | Disease → Disease | 项目定义的五阶段顺序，背景证据 |
| ENCODES | Gene → Protein | STRING 记录的映射，背景证据 |
| INTERACTS_WITH | Protein → Protein | STRING 互作，背景证据 |
| PARTICIPATES_IN | Gene 或 Metabolite → Pathway | 官方成员记录和明确的化合物映射，背景证据 |
| EXPRESSED_IN | Gene → Tissue 或 CellType | HPA 表达，背景证据 |
| ASSOCIATED_WITH_METABOLITE | Gene → Metabolite | HMDB 关联，背景证据 |
| IS_A | 实体 → 类或类 → 父类 | 类层级，背景证据 |
| SUBPATHWAY_OF | Pathway → Pathway | Reactome 和 KEGG BRITE 层级，背景证据 |
| CROSS_REFERENCE | Pathway → GO 参考标识 | Reactome GO 生物过程注释，背景证据 |
| STAGE_DIFFERENTIAL | Gene → 变化发生的目标 Stage | DisGeNET 五阶段集合差异，inferred_or_hypothesis |

2B 的专用谓词在候选关系签名、证据卡、写入契约、离线对齐和加载器中统一。v3 读取历史抽取结果时，将旧的 Metabolite → Disease 类型映射到专用谓词。原文明确支持的文献代谢物–疾病发现依现有语义和写入门禁判定。骨干的 HMDB 边保留背景证据标记。

## 通路和代谢物的纳入规则

- 通路沿祖先方向从 v2 的人类通路向上保留闭包。KEGG BRITE 分类节点用稳定的分类路径标识。同一条 Reactome 关系可能有多个父通路。跨库注释使用 Reactome 提供的 Pathways2GoTerms_human 文件。 [Reactome 下载](https://reactome.org/download-data) [KEGG BRITE br08901](https://www.kegg.jp/brite/br08901)
- 保留 v2 代谢物的 HMDB 主标识。严格纳入条件同时要求 HMDB 中存在骨干基因酶或转运体记录，以及精确匹配五阶段之一的疾病记录。HMDB 蛋白通过精确基因符号或 UniProt 匹配，冲突结果需审核。
- 扩大纳入条件要求 HMDB 化学分类属于胆汁酸、脂肪酸或神经酰胺，并有明确的肝胆通路映射。一般脂质途径还需 HMDB 的肝脏组织记录或五阶段疾病记录。数据中的 liver_pathway_ids 和 selection_routes 保存纳入依据。KEGG 和 Reactome 通路边保留所用的化合物或 ChEBI 标识。
- 化学分类沿 HMDB 的 kingdom、super_class、class、sub_class 构建。基因边保存 HMDB 蛋白角色。疾病边在原始记录提供时保存疾病名、PMID 和 OMIM ID。 [HMDB](https://hmdb.ca/)

## 阶段画像

stage_profile 是长度为五的整数列表，按 Stage 1–5 排列。每个正计数对应 stages 列表中的一项，first_stage 和 last_stage 记录范围。

- 基因使用每阶段的 DisGeNET 记录计数。early_only 对应 1–2，late_only 对应 4–5，persistent 覆盖全部五阶段，其余非空组合为 transition，无阶段记录时为 unmapped。
- 通路记录 direct_backbone_gene_count 和包含子通路的去重 backbone_gene_count。各阶段的数值是参与该通路且有该阶段直接疾病记录的基因数。
- 代谢物使用明确关联到阶段的 HMDB 疾病记录。阶段差异用相邻阶段基因集合之差，change 取 added 或 removed，每条关系保存 from_stage 和 to_stage。

## 文献对齐与入库

C/D 通过 --backbone-data 指定离线快照，保留旧的 --v2-data 命令参数。对齐使用类型内精确名称、别名和标准标识，冲突保留为候选。novelty 区分同种关系已知、两端有别的关系、新关系和端点未对齐。带 Stage 端点的文献边保存 stage_order 和 stage_orders。

加载前核对库中的骨干快照 ID。SAME_AS 匹配真实骨干主键。未对齐的根概念挂到所属顶层。已对齐概念沿用骨干节点的层级位置。每条 AUTO 文献关系写到概念之间，两端都已对齐时也写到骨干节点之间。关系的 evidence_uids 对应 Evidence 节点中的 PMID、原句、字符位置。

~~~bash
liverkg ontology load --run-id BATCH --uri bolt://127.0.0.1:17687 --database neo4j --dry-run
liverkg ontology load --run-id BATCH --uri bolt://127.0.0.1:17687 --database neo4j --yes
liverkg ontology unload --run-id BATCH --uri bolt://127.0.0.1:17687 --database neo4j --dry-run
liverkg ontology unload --run-id BATCH --uri bolt://127.0.0.1:17687 --database neo4j --yes
~~~

撤回按 run_id 删除批次的文献节点和边。多批次引用同一 PMID 时，Article 由各自的 IN_RUN 关系维持，最后一个引用撤回时清理孤立 Article。
