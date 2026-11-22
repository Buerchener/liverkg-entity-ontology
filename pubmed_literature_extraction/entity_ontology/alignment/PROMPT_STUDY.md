# Alignment prompt study

This study improves the alignment request without changing candidate retrieval,
model parameters, the output validator, or mapping admission rules. A valid JSON
response is not necessarily a correct biomedical mapping. Previous live samples
are development data, including the nine deliberately difficult examples.

## Upstream patterns inspected

Source code was inspected on 2026-11-22 and pinned to the following commits. We
adapted organization patterns and wrote our own wording; we did not copy their
domain examples or use evaluated biomedical examples in the prompts.

| Project and source | Pattern used | Limitation |
| --- | --- | --- |
| [Graphiti entity deduplication](https://github.com/getzep/graphiti/blob/9b8a4630092084734b080f2e6f409c2c23bdcc3a/graphiti_core/prompts/dedupe_nodes.py) | Distinguish identity from relatedness, use supplied candidate IDs, explicitly allow abstention, require full ID coverage. | Its identity task does not establish biomedical class/individual or species semantics. Apache-2.0. |
| [Microsoft GraphRAG extraction](https://github.com/microsoft/graphrag/blob/34b2b67ae4601ad0b47bcd179fbe3f2a23aa0857/packages/graphrag/graphrag/prompts/index/extract_graph.py) | Separate goal, procedure, output instructions and input evidence. | General relationship extraction is broader than identity; its text delimiters do not guarantee JSON compliance. MIT. |
| [Instructor validation/retry](https://github.com/567-labs/instructor/blob/0acb3a19c3ec6f89c6a204340c52a30ee33fa04b/instructor/v2/core/retry.py) | Preserve failures, validate in code and bound retries. | Runtime validation and constrained decoding are distinct from prompt wording. MIT. |

## Frozen comparison

Artifacts are local under `runs/alignment/prompt-study-v1/`; no graph is written.
`baseline/manifest.json` freezes the original source files and prompt. Historical
journals are not overwritten. `experiment_plan.json` records the selection rules
before model trials. Every prompt and request payload is hashed.

The model remains `qwen3.7-plus`, temperature 0, max_tokens 6000, thinking disabled.
The API response format remains `json_object` for every variant. In the `schema`
variant, JSON Schema is text inside the prompt, not provider-enforced decoding.
There are no evaluated names, node IDs, biomedical examples or expected answers
hardcoded in any new prompt.

The first development cohort has 16 frozen requests: two original requests per
entity type plus two requests containing previously recorded OLS observations,
selected by payload hash before trials. All variants receive identical input
payloads. Lookup and candidate retrieval are not rerun during this comparison.
One correction attempt is allowed, using the existing correction mechanism.

After development, the selected prompt is frozen before unseen real KG payloads
are opened. The unseen cohort excludes all 42 prior KG entities. Any held-out
failure is reported; it is not used to revise the selected prompt in this study.

## Measurements

- First-attempt success under the original schema, complete candidate-ID coverage
  and exact local-quote validator.
- Success after at most one correction, with all failed responses retained.
- Calls, returned token usage and latency, including unsuccessful attempts.
- Unsupported semantic assertions and source-based review of changed decisions.
- Positive proposals with absent or one-sided local evidence, so empty citations
  cannot masquerade as an improvement.

Counts use requests as the denominator, separately from unique KG entities and
candidate pairs. More `MAPS_TO` proposals or more syntactically valid output are
not evidence of higher biomedical accuracy. The source-based review is performed
by assistants, not an independent expert gold standard.

## Reproduce

The evaluation CLI is `python -m entity_ontology.alignment.prompt_evaluation`.
Use the frozen corpus and configuration, retain each run's output directory, and
never combine a changed prompt with an old run's metrics. Cached trial responses
may be replayed without another model call; a genuine replicate uses a new output
directory. Final measured results and the selected default follow.

## 实测结果与当前默认版本

已比较五个版本：`legacy`、`sections`、`schema`、`grounded`、`verified`。
模型固定为 `qwen3.7-plus`；三轮开发实验始终使用同一组 16 个请求及原校验规则。
下面的分母是请求数量，不是对齐成功的实体数量。

| 开发轮次 | 提示词 | 首次通过 | 最多一次重试后通过 | 调用数 | 总 tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | legacy | 7/16 | 10/16 | 25 | 62,175 |
| 1 | sections | 13/16 | 14/16 | 19 | 49,953 |
| 1 | schema | 13/16 | 14/16 | 19 | 51,561 |
| 2 | legacy（新调用复测） | 8/16 | 12/16 | 24 | 62,339 |
| 2 | sections（新调用复测） | 13/16 | 13/16 | 19 | 50,110 |
| 2 | grounded | 16/16 | 16/16 | 16 | 45,760 |
| 3 | legacy（新调用复测） | 6/16 | 12/16 | 26 | 66,656 |
| 3 | grounded（新调用复测） | 16/16 | 16/16 | 16 | 46,619 |
| 3 | verified | 16/16 | 16/16 | 16 | 48,703 |

第一轮改进了任务、证据和输出规范的组织方式。第二轮提供可替换的引用语法模板，明确
元数据与本地引文的边界，格式达标，但助手依据来源的复核发现一个新的不可靠身份提议。
因此在尚未打开未见数据时增加第三轮，细化通用的身份依据：两端实际共有的标准 ID、
分别属于当前两个端点的已核查外部记录，或两侧有信息量且范围一致的本地定义。
新规则没有加入测试实体名称、ID、答案或生物医学案例。

第三轮的助手资料复核中，`verified` 的不可靠原始正向提议为 3 条，少于该轮
`legacy` 的 4 条和 `grounded` 的 6 条；这些提议仍被原程序规则拦截。`verified`
保留了有来源支持的正例，并非一律弃权。它仍有不可靠原始提议和理由/标签不一致的问题，
因此选为本次候选中较好的折中，而不是宣称最优或已达到专家准确率。

`selection.json` 在打开未见数据前冻结了 `verified`。之后只评估，不再改写提示词。
未见组有 14 个新 KG 节点、18 个请求：14 个初始请求和 4 个含真实 OLS 记录的请求。
它们排除了先前用过的全部 42 个 KG 节点；重复上下文不算独立实体。

| 未见数据 | 首次通过 | 最多一次重试后通过 | 调用数 | 总 tokens |
| --- | ---: | ---: | ---: | ---: |
| legacy | 14/18（77.8%） | 18/18 | 22 | 64,634 |
| verified（冻结版本） | 18/18（100%） | 18/18 | 18 | 63,690 |

实测改善主要是首次校验稳定性：旧版需要 4 次纠正，新版无需纠正。不能把表中的 100%
读作匹配准确率。未见组的两个版本都提出过 2 条依据不足的 `MAPS_TO`，均被程序拦截，
没有获准映射。助手资料复核中，新版减少了类别方向错误，但增加了部分证据不足却判为
`UNMATCHED` 的情况；语义质量不能只用格式成绩概括。该组没有足以验证身份匹配的正例，
不能据此估计正向匹配召回率，也没有独立专家金标准。

实验总共实际调用对齐模型 220 次，返回用量合计 612,200 tokens；不包括 Embedding
或只读 OLS 查询。额外的 16 个真实响应通过生产 `AlignmentModel` 离线回放全部成功，
这是接入检查，不是新的模型试验。全部 103 项代码测试通过。

当前默认提示词为 [prompts/verified.txt](prompts/verified.txt)。可明确指定：

```bash
python -m entity_ontology.alignment run \
  --ontology <existing-placement.json> --kg-snapshot <kg-snapshot.json> \
  --config <model-config.json> --output <new-output-directory> \
  --alignment-prompt verified
```

所有请求、回答、失败、纠正、用量和提示词哈希保存在
`runs/alignment/prompt-study-v1/{round1,round2,round3,heldout}/`。
来源复核和实验完整性报告与结果分开保存；旧基线和先前映射结果均未覆盖。
Ontology 构建核心和 Neo4j 数据没有修改。
