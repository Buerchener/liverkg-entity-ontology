# Evidence-v4 公开实验结果

以 Qwen3.7-Flash 处理 105 个真实 LiverKG 节点。独立复核后，62 个新概念和 1 个既有概念建立了映射，保留了 47 条具体父类关系。

- [完整中文实验报告](report.zh.md)与[各组统计](cohort-metrics.json)
- [105 个逐例结果](cohorts/refined-scale-flash.json)、[复核后的 470 个本体节点与 510 条边](reviewed-scale/ontology.json)、[63 条新增映射](reviewed-scale/mappings.json)、[99 条新增边的证据](reviewed-scale/edge-evidence.json)和[复核后的统计](reviewed-scale/summary.json)
- [Plus 疑难案例](cohorts/refined-scale-difficult-plus.json)、[诊断用独立图](reviewed-plus-diagnostic/ontology.json)、[映射](reviewed-plus-diagnostic/mappings.json)与[逐边证据](reviewed-plus-diagnostic/edge-evidence.json)
- [定义调查](validation/definition-cases.json)、[规模组 23 条定义缺口](validation/definition-gaps.json)、[关键病例复核](validation/scope-and-format.json)、[费用台账](api-cost-ledger.md)和[原文件及公开副本哈希](manifest.json)

case_id 是跨组一致的公开案例码。name 与 kind 给出输入名称和类型，identifiers 和 links 指向可查询的公共来源。status 与 maps_to 表示自动流程输出。规模组另有 reviewed_status 和 reviewed_maps_to 记录复核结论。同一病例可能出现在不同版本或模型的组中，计数见各组分母。

图的 child_id 指向 parent_id，added 区分本次和原有边。新增边可按两个端点和 case_id 与证据文件关联。schema_root 为 true 表示本地类型归属。具体父类的 authority_path 列出原始命名子类断言。化学角色单独记录。源记录给出的证据仍可能存在粒度或物种范围限制。

发布包保留概念名、公开标识符、判定、来源引用及实验统计。完整文献、原始数据库记录、模型输入输出及环境配置保存在实验环境中。图省略了来源文本等大字段，映射文件列出本次新增映射。manifest.json 分别列出原文件与公开投影的 SHA-256，可核验各自版本。
