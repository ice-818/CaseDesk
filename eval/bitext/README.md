# Bitext 中文客服意图评测

使用 Bitext 官方英文训练数据的中文翻译，评测 CaseDesk 的跨数据源意图分类。数据为合成客服请求，中文由同一 DeepSeek 模型翻译并进行第二轮机器审核。指标包含待人工确认的数据，正式使用前需核对审核清单。

## 来源与标签

来源为 https://github.com/bitext/customer-support-llm-chatbot-training-dataset ，固定提交为 72ea2203180d14a416b579c657033ecd21e59a11，数据版本 v11，26,872 行。原始许可证 CDLA-Sharing-1.0 完整保存在 LICENSE.txt，翻译、重新标注、抽样属于本项目修改。归属 Bitext。

manifest.json 记录 CSV SHA256、来源、许可证、抽样种子20261111、全部映射和无法映射类别。最终抽样按13个可映射外部类别等额分层，500条覆盖6个本地标签。其余8个本地标签的支持数为0。本评测的分布并非真实线上流量分布。

- cancel_order 对应 cancel_order。
- track_order 对应 order_inquiry。物流轨迹与订单状态边界需人工核对。
- get_refund、track_refund 对应 refund。
- complaint 对应 complaint。
- contact_human_agent 对应 human_handoff。
- check_refund_policy、check_cancellation_fee、delivery_options、delivery_period、check_invoice、get_invoice、check_payment_methods 对应 faq_policy。其中具体账单操作与通用政策的映射较宽，审核清单标注歧义。

账户管理、支付故障、下单与修改订单等14类缺少明确匹配标签，单独排除，明细在manifest.json。不能将这些类别当作out_of_scope金标。

## 数据与划分

sample.json 保留原英文、官方行号id、外部标签与本地标签。translated.json 和 audit.json 保存翻译及机器审核。human_review.json 单列翻译和标签需要确认的样本。

frozen.json 包含最终500条中英样例、分组、审核标记和200/300划分。split_stats.json 保存文件SHA256、分组数及各类分布。英文和中文均经标准化完全去重，中文重合时同类别补抽，补抽种子20261112。前次未完成评测因14条中文重复已归档在discarded-*目录，最终结果只使用重新冻结的题集。

相似问法按连通分量分组。英文/中文完全相同，或同外部标签下英文词集合Jaccard≥0.70、英文/中文SequenceMatcher≥0.85时归为同组。固定种子打乱组，通过子集和选择完整组得到200条调试集和300条测试集。分组优先于分层比例，两个子集的类别比例有差别。启发式相似度无法完全识别语义同义问法。

## 运行

在项目根目录PowerShell执行。

```powershell
.\.venv\Scripts\python.exe -m eval.bitext prepare
.\.venv\Scripts\python.exe -m eval.bitext translate
.\.venv\Scripts\python.exe -m eval.bitext validate
.\.venv\Scripts\python.exe -m eval.bitext evaluate
```

translate与evaluate使用现有模型配置，会发送公开样例至DeepSeek官方API。usage.json持久记录实际尝试数、成功响应token数与网络失败。累计硬上限1900次，单次最长60秒，并发最多4，单次请求没有自动重试。缓存和断点保留在当前目录，重复执行会复用完成结果。若修改模型、题集或提示词，应另建完整实验目录后重跑，不能混用旧缓存。

当前分类器使用原始intent_v3。先在200条调试集上运行，再仅将调试集错例交给模型生成一次候选提示词，并记录SHA256。候选冻结后才运行保留测试集的基线和候选版本。候选只用于本次评测。分别统计覆盖层关闭与开启结果，复用同一次原始模型分类，覆盖层不额外调用模型。请求或解析失败按ERROR计入主指标，不将规则兜底当成模型成功。

report.json 包含准确率、每个本地类别Precision/Recall/F1及支持数、支持类别宏平均F1、混淆矩阵和错题。另报告排除待审核样本后的准确率，属于敏感性分析。机器复核不代表人工金标。

## 兼容与后续

原eval.runner、metrics.py、原题集及生产提示词保持原入口。legacy_compat目录保存旧100条规则意图入口的独立验证结果。tests/unit/test_bitext.py覆盖分组隔离、跨标签完全重复以及失败计分。

后续检索数据源参考 https://github.com/project-miracl/miracl ，MIRACL中文语料约493万段，计划先固定独立中文开发子集、语料版本及qrels，使用独立索引和命名空间，单列数据集、语料数量与查询数量，报告Recall@k、MRR与nDCG。抽取缩小语料时需明确报告子语料难度。

证据不足数据源参考 https://github.com/project-miracl/nomiracl ，分开采样relevant与non_relevant，分别报告错误拒答和错误判断有证据的比例，固定中文配置与独立上下文/索引。第一阶段未运行这两个数据集。
