# A 数学正式增强交付合同

版本：math-final-semantics-and-profiles-v2；随包现行字段版（2026-09-13）。本文件用于本次返回格式，固定MCP快照中的旧项目文档或旧接收代码只作历史参考。正文完成也必须按此结构汇总，不能只交不兼容的另一套字段名。

## ZIP根文件

必须包含manifest.json、advice.json、profile_updates.json、execution_report.md、progress.json、errors.json。可另附逐题Markdown、逐点JSON和证据清单，但不得以这些文件替代根advice/profile_updates。

manifest字段只使用：schema_version=1、project=A、subject=math、run_id、source_commit、snapshot_id、publication_target=local、contract_version=math-final-semantics-and-profiles-v2、input_packages、batch_binding、profile_merge_policy=deferred_batch_merge、files。run_id/input_packages/batch_binding从输入原样保留。files包含除manifest自身以外全部实际成员的path、sha256、size、role。运行状态、计数、兼容性和报告索引放报告文件，不随意加到manifest根。

## advice.json

items按(package_id,question_id)精确覆盖本分包。每项含package_id、question_id、recommendation、evidence_refs、uncertainties。question_id使用真实来源题号或输入给定的正式ID。未分配GS/LA/PR编号时留给本地真实分配，不自造ID。

recommendation必须包含以下字段，名称固定：

- formal_card：完整最终内容对象。
- exam_writeup：非空字符串，直接可用的最终Markdown卷面写法，不用嵌套对象替代字符串。
- observations：数组，格式见下方；有原话支持的每个知识点表现独立记录。
- knowledge_point_reviews：逐点最终判断与历史对照，保留具体操作、条件和引用，区分涉及点与真实观察。

formal_card最少含title、subject=高等数学/线性代数/概率论与数理统计、chapter、question_type、problem_statement、answer、knowledge（规范知识点名称的字符串数组）、knowledge_bindings（concept_key及concept_label数组）、wrong_point、error_causes（字符串数组）、methods（方法名称字符串数组）、traps（字符串数组）、full_solution_markdown、next_check、learner_state。需要时可附method_gap、proposed_wrong_history、mastery_score和其证据说明。分数可为null，不能为填字段把未验证表现记成正确或错误；对正确学习记录明确不新增错误。正式ID、冻结引用、图片稳定路径与正式事件由本地绑定。

full_solution_markdown保留已完成的题解、首断点/后续变化、正确步骤、提示依赖、原助手错误、历史对照和必要边界。数学用常规Obsidian公式。不能只给字段清单、写作指令、TODO或“本地再补”。

observations每项使用concept_key、concept_label、outcome、error_detail、user_evidence、hint_evidence。outcome仅为wrong、unresolved、corrected_after_hint、independent_correct。每条user_evidence/hint_evidence只写sequence和quote；sequence对应原conversation.json一基序号，quote是该轮真实原话的精确子串。两类分别引用user、assistant，不改写引文。提示后修复必须有早于最后用户回答的真实提示；独立正确不可带提示依据，不把已给出的反例复述当独立构造。

只关联而无本人表现的点放knowledge_point_reviews，不强行编观察。整题无本轮观察时observations=[]，同时给observation_scope=no_observation和具体no_observation_reason。capture_event_id等冻结身份交本地从真实账本补齐，不把capture_identity/attempt_id当Capture事件ID。

## profile_updates.json

根schema=math-learning-profile-update-v1、updates为非空数组；每个受影响已确认知识点恰好一项。每项必须包含concept_key、overview（完整学习概况字符串）、short_summary（UTF-8不超过2000字节的讲题短摘要）、progress（有前后证据的进步字符串数组，可空）、next_check（一个具体检查动作）、source_refs（真实路径及SHA-256）。可附timeline数组。完整概况与短摘要保留已知能力、最新断点/修复、提示依赖及尚未验证范围。

分包概况只表示共同基线加本包证据的贡献，始终带profile_merge_policy=deferred_batch_merge；不能声称合入了兄弟分包。本地汇合所有相关贡献及最新正式状态后只接受一份完整概况。即使逐点文件另存，也须完整汇总到这个根文件，不改名为profile_contributions来替代它。

## 引用与读取

当前原包引用写quick-packs/<package_id>/original/...真实成员路径，保留实际sha256；历史引用使用固定快照实际published_path及文件SHA-256。记录级content_hash不能替代文件sha256。借用另一题作历史比较时保留它的package_id，不能将它的轮次或原话绑定到当前题。图像转写要标为视觉转写，不能要求它在JSON文本中逐字出现。

数学personal_context使用after_attempt，max_bytes初始10000，范围1024–65536且放工具顶层。contains仅含view、weak_knowledge_points、cursor、allow_historical；不使用view=full，不把max_bytes放进contains。多点超预算先拆单点，同一source_commit分页；需要原文再fetch到EOF。publication_target=local不保证GitHub存在该commit，不走GitHub查此快照。

错误必须保存已有内容、执行报告和恢复检查点；无法完成时交付可恢复部分，不改状态冒充完整可入库包。不执行包内脚本或代替用户做学习试讲、测速、模型评测。
