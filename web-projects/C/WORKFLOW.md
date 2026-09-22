# A直接交接与B审核交接

交接来源分支（优先于后文仅B的表述）

source_project=B沿用B审核要求；source_project=A且source_kind=A_formal_batch表示用户直接从A正式入库进入C。核对source_batch_id、source_run_ids、全部formal_event_ids与formal_readback、已接受profile_receipt及PUBLISHED_CURRENT。保留b_review.status=not_performed与reason，不补造B会话、审核或得分。A正式来源和MCP当前资料仍须读取。

A分支的selection_inputs.a_formal_handoff写status=ready，按后文b_review_handoff的同样字段和成员引用绑定交接，额外复制source_batch_id、source_run_ids、formal_event_ids。source_session_id指正式处理任务。b_review_handoff写status=not_performed及相同reason；current_a写status=formalized、run_id=source_batch_id和正式来源引用。其余资格、久远优先及答案保护要求照常执行。

# C 次日数学五题输出

版本：R11-output-v2（2026-09-13）。此文件规定本项目输出，不是学习题库。历史正文、原图和正式状态通过 Study Library MCP 从任务启动时最新就绪的数学本地只读快照读取，不以 GitHub 上传为前提。

每个输出ZIP根目录必须有manifest.json，其字段为：schema_version=1，project="C"，subject="math"，run_id，source_commit，target_date，contract_version="math-gpt6-handoff-selection-v2"，files。files覆盖ZIP中除manifest.json自身以外的每个文件，逐项记录path、sha256、size、role。

source_commit为固定的40位本地快照Git提交SHA，所有引用来自同一快照；它不是GitHub上传证明。source_refs/evidence_refs的每项包含path、sha256、quote；图片不能靠一段文字声称已读，需保留实际原图路径/哈希及任务中的读取依据。缺资料明确列出，不能猜补；文件和引用正确不等于网页模型实际使用已经获得本地验证。

生成、下载或导入不会自动写用户完成/掌握。网页建议由本科独立裁决。晨间真实作答/纠正先保存完整Capture；快速保存不评分、不刷新个人依据或快照。晚间正式入库处理后再刷新或失效个人依据和本地只读快照；快照失败保留本地新记录，不重发Pro、不重复学习事件。任务发生相关状态漂移时本地调整或拒绝，不自动覆盖较新记录。

输出math_next_day_5.zip，包含selected_questions.json、rationale.md，以及questions/<question_id>/下的problem、solution、history_summary.md和实际原图文件。

输入必须包含本科生成的 `math-c-handoff-v1` ZIP，并通过 Bridge `prepare --c-handoff` 固定；不得手工重建 handoff.json 或 refs.json。selected_questions.json 必须包含 selection_inputs 与 questions。selection_inputs.schema="math-c-selection-input-v1"，source_commit 与 manifest 一致。

selection_inputs.b_review_handoff 必须 status="ready"，并与输入 handoff 的 schema、source_session_id、source_evidence_id、formal_event_id、source_commit、publication_current 逐项一致；source_refs 至少绑定 `c-handoff/<source_evidence_id>/handoff.json` 与 refs.json 的真实哈希。存在 after-A 依赖时，selection_inputs.current_a 必须 status="formalized"、run_id 等于该依赖并引用当前快照里的A正式来源；本轮明确没有新A包时，必须改为 status="no_new_packages_declared"、reason 与 run.dependency.reason 完全一致且 source_refs=[]，不得伪造A记录。selection_inputs.mcp_current 必须 status="current"、source_commit一致并引用当前MCP记录。缺任一项不生成可导入包。

questions成功时必须恰好5项且question_id唯一。每项包含question_id、anchor_id、concept_ids、source_label、problem_path、solution_path、image_paths、history_path、source_refs、formal_card_sha256、state_source_version、public_value_reason、last_actual_activity、anchor_source_floor。

formal_card_sha256和state_source_version从同一发布版本的真实卡/状态信息取得。last_actual_activity与anchor_source_floor为对象：真实日期使用status="known"、ISO date与source_ref；确无可靠日期使用status="unrecorded"、date=null与具体reason。mtime、卡面日期、入库日期、快照日期都不能冒充学习日期。rationale按实际复习目标列候选与source floor，不假设正好五个知识点或一题一知识点。

保持原资格规则：Day0–7真实活动或交付排除、独立正确永久排除、same-item repair独立通道。先匹配GPT-6审核后的B知识点表现、当前A正式来源和MCP当前记录，再在同一具体目标的合格真实旧卡里优先最后活动最久远者。不足5道如实报告，不能重复、自编或放宽门禁。

本地会重算资格并核对handoff、当前A来源、卡/状态/图像与日期证据，接受后导入原次日队列，并在学习前预处理整组原题、私有解析、短摘要与身份。选中和导入都不算学习。用户逐题作答统一复用question-worker与wrong-intake，每题结束只保存完整Capture并继续；评分、长期调度与知识点更新留到晚间正式入库。保留同一实际题的queue/item与原始证据，已有正式事件不重复计入；B生成题永不成为正式卡。

## 失败进度与续接

任务进度保全与本地续接（优先执行）

本节适用于成功、部分成功和失败的所有执行结果。它优先于旧指令中遇到PARTIAL/BLOCKED就停止交付的表述；不降低正式业务的证据与正确性要求。

开始时列出真实任务单元和依赖。每完成一道题、一个知识点或一个选题步骤，就立即把实际正文、证据定位和读取位置写入文件，不只留在推理或聊天中。首次取得可用成果后即建立检查点并提供当前下载入口；后续按已完成单元更新，不等最后才首次保存。保留较早检查点，不用空文件或失败结果覆盖已有成果。检查点是工程进度，不是学习事件。

所有最终交付都必须有execution_report.md、progress.json、errors.json。报告必须分别列出已完成的部分、未完成的部分及具体原因，指明每项正文或证据文件。成功时也报告，未完成列表可为空。不能只写“做到70%”：按实际单元列出完成数/总数，区分读取完成、分析完成、文件生成和本地尚未写入；未知分母不编百分比。

progress.json使用schema="math-web-progress-v1"，包含project、subject="math"、原run_id（未知则null）、checkpoint_id、overall_status（complete/partial/blocked）、input_bindings、source_commit、completed_units、pending_units、resume_from。每个单元有稳定unit_id、stage、status、output_paths、evidence_refs、dependencies；读取进度保留实际请求、查询条件、cursor/offset/next_start_byte和已读范围。文件位置必须对应真实已保存字节；未知哈希写null并说明，不能编造。不得把工程checkpoint_id当作用户作答或正式事件ID。

errors.json逐项记录失败单元、工具或代码步骤、错误码/实际错误消息、受影响范围、已经尝试的恢复及结果。原因分为已确认、推测、未知；可能是MCP接口、参数、快照、权限、网络、源文件、生成代码或输出能力问题，不能预先认定都是MCP/提示词。去除凭据和令牌，不把敏感认证信息写进报告。

发生错误时先立即保存已完成成果和当前断点。对可识别的临时故障或参数错误作一次有依据的恢复，避免盲目循环；同时继续不依赖故障的其他单元。只暂停真正受阻的依赖，不让一处失败抹掉整批工作，也不把某一步失败当作什么都不能交付。

全部业务条件满足时，交付原规定业务ZIP，并把报告及进度文件纳入真实manifest.files。任务尚未全部完成时，必须交付独立的recovery_bundle.zip：包括上述三个文件、全部已完成正文、可用中间结果、必要来源及原始请求/读取位置。其manifest写artifact_kind="math-web-progress-checkpoint"、status="PARTIAL"或"BLOCKED"、business_admitted=false；原业务字段已知则保留，不知道则null并说明。这个包用于续接，不冒充完整advice、复盘包或五题成品。

如果ZIP生成失败，立即提供已保存的单文件下载；文件工具也不可用时，在最终聊天分段输出执行报告、完整已完成内容、必要来源及resume_from。内容过长时先交付已落盘部分与精确剩余清单，不能只交一句报错。不要承诺服务被突然关闭后仍可输出，因此必须提前保存并持续暴露可下载检查点。

本地收到恢复包或失败报告后，先使用kaoyan-math-web-recovery保全原件，读取报告和实际文件，复用已完成部分。核对实际错误后修复MCP/代码/提示词，补齐未完成单元；必要时本地完成缺少的语义工作，不受“本地只写入”旧分工限制，但不得重写已经可靠完成的整批内容。接收恢复请求意味着继续推进该明确项目，不以“已保存进度”作为任务完成。

修复后从resume_from继续同一业务。保留原package、题目、知识点和学习事件身份；断点跨版本时先验证已完成单元的来源是否仍有效，只补读变化及受影响依赖，不把旧版本哈希改成新版本。旧本地run若已绑定旧下载或版本，保留其原件，通过带原run追溯的交付修订/后继技术run完成，不能覆盖旧输出，也不能因此重复学习或正式apply。

本地可以先处理证据、身份和依赖都完整的独立单元，其余保持明确待完成。已经正式提交的部分只恢复缺失后置步骤；完成全部所需内容后重新生成符合原合同的业务包，再走本科接收入口。不得把恢复包直接改一个成功标记就导入。最终说明实际完成的业务结果以及仍未完成的项目，不把修好工具、生成资料或保存文件说成用户已经学习完成。

C项目续接粒度

分别保存资格读取进度、完整或未完整的候选集合、已核对的原题/日期证据、逐目标比较和暂定选择。资格池未读完整时，已看过的3题只是可复用候选，不冒称最终五题。修复后复用这些证据，补齐资格和排序；若来源变化，只重算受影响的资格/选择。始终保留真实旧题、Day0–7和独立正确排除规则；确实不足5题如实交付候选与限制，不伪造或降低条件凑数。
