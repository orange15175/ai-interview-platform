from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_BREAK
from pathlib import Path

OUT = Path(r"C:\Users\15175\Desktop\AI面试系统四项优化实现方案.docx")

def set_cell_shading(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = tcPr.find(qn('w:shd'))
    if shd is None:
        shd = OxmlElement('w:shd')
        tcPr.append(shd)
    shd.set(qn('w:fill'), fill)

def set_cell_text(cell, text, bold=False, color=None, size=9.5):
    cell.text = ''
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(str(text))
    r.bold = bold
    r.font.size = Pt(size)
    r.font.name = 'Microsoft YaHei'
    r._element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

def add_table(doc, headers, rows, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = 'Table Grid'
    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        set_cell_text(hdr[i], h, bold=True, color='FFFFFF', size=9)
        set_cell_shading(hdr[i], '1F4E79')
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            set_cell_text(cells[i], value, size=8.8)
            if len(table.rows) % 2 == 0:
                set_cell_shading(cells[i], 'F3F6F9')
    if widths:
        for row in table.rows:
            for i, width in enumerate(widths):
                row.cells[i].width = Inches(width)
    doc.add_paragraph().paragraph_format.space_after = Pt(1)
    return table

def add_bullet(doc, text, level=0):
    p = doc.add_paragraph(style='List Bullet' if level == 0 else 'List Bullet 2')
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.15
    p.add_run(text)
    return p

def add_code(doc, text):
    p = doc.add_paragraph(style='Code Block')
    p.paragraph_format.space_after = Pt(5)
    p.add_run(text)
    return p

def add_note(doc, label, text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.2)
    p.paragraph_format.right_indent = Inches(0.2)
    p.paragraph_format.space_after = Pt(6)
    r = p.add_run(label + '：')
    r.bold = True
    r.font.color.rgb = RGBColor(31, 78, 121)
    p.add_run(text)
    return p

doc = Document()
sec = doc.sections[0]
sec.top_margin = Inches(0.65)
sec.bottom_margin = Inches(0.65)
sec.left_margin = Inches(0.78)
sec.right_margin = Inches(0.78)

styles = doc.styles
normal = styles['Normal']
normal.font.name = 'Microsoft YaHei'
normal._element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
normal.font.size = Pt(10.5)
normal.paragraph_format.space_after = Pt(5)
normal.paragraph_format.line_spacing = 1.18
for name, size, color in [('Title', 22, '1F4E79'), ('Heading 1', 16, '1F4E79'), ('Heading 2', 12.5, '2F5597'), ('Heading 3', 11, '404040')]:
    st = styles[name]
    st.font.name = 'Microsoft YaHei'
    st._element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    st.font.size = Pt(size)
    st.font.color.rgb = RGBColor.from_string(color)
    st.font.bold = True
    st.paragraph_format.space_before = Pt(10 if name != 'Title' else 0)
    st.paragraph_format.space_after = Pt(5)
if 'Code Block' not in styles:
    code = styles.add_style('Code Block', WD_STYLE_TYPE.PARAGRAPH)
else:
    code = styles['Code Block']
code.font.name = 'Consolas'
code._element.rPr.rFonts.set(qn('w:eastAsia'), 'Consolas')
code.font.size = Pt(9)
code.font.color.rgb = RGBColor(40, 40, 40)
code.paragraph_format.left_indent = Inches(0.2)
code.paragraph_format.right_indent = Inches(0.2)
code.paragraph_format.space_before = Pt(2)
code.paragraph_format.space_after = Pt(5)

# Title page content
p = doc.add_paragraph(style='Title')
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run('AI面试系统四项优化实现方案')
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('面向其他模型的工程实现说明')
r.font.size = Pt(12)
r.font.color.rgb = RGBColor(89, 89, 89)

doc.add_paragraph()
add_note(doc, '文档目的', '在当前 Python、FastAPI、LangGraph、PostgreSQL、Redis 项目基础上，增量实现面试复盘报告、数据库级写入隔离、失败提交后台补偿和分层模型降级。本文可直接作为编码任务说明交给其他模型。')
add_note(doc, '实施原则', '先阅读现有代码和迁移记录，再按现有命名、仓储层和错误处理方式实现。不得删除或弱化现有的会话归属校验、request_id 幂等、Redis 锁、state_version CAS、Checkpoint 恢复和 rubric 隐藏逻辑。')
add_note(doc, '重要边界', '本方案不要求虚构性能指标，不使用默认分数代替模型评分，不把模型超时表述为严格 exactly-once。每项能力都必须有状态记录、错误码和自动化测试。')

h = doc.add_heading('一 项目现状与本次范围', level=1)
doc.add_paragraph('当前系统已经具备简历解析、岗位匹配、个性化出题、逐题评分、规则化追问、Redis 会话锁、请求幂等、状态版本控制和 Redis Checkpoint 恢复。本次优化的重点是把已有能力进一步产品化和工程化。')
add_table(doc, ['优化项', '解决的问题', '新增或修改的主要边界'], [
    ('面试复盘报告', 'records 只能逐题展示，无法总结能力表现', '报告服务、报告接口、统计规则、权限和测试'),
    ('write_epoch', '旧 Worker 在锁失效后可能迟到写入', '会话表字段、写入条件、所有关键写路径'),
    ('失败提交补偿', '失败提交主要依赖用户手动重试', '重试字段、任务抢占、后台 Worker、退避和冲突处理'),
    ('模型分层降级', '解析、追问、评分失败时的业务语义不统一', '错误码、状态流转、可重试边界、降级记录'),
], [1.2, 2.15, 3.55])

h = doc.add_heading('二 总体实施约束', level=1)
for t in [
    '先使用 rg 检查现有模型、迁移、仓储、服务、API 和测试，不得凭空创建与当前项目重复的抽象。',
    '数据库结构必须通过 Alembic migration 变更，禁止只改 ORM 模型而不提供迁移。',
    '模型调用不应放在数据库长事务或数据库行锁持有期间。',
    '报告默认只使用已经落库的评分，不因生成报告再次调用模型。',
    '后台补偿与用户手动重试必须共用同一套状态判断和原 request_id。',
    '所有降级都必须有稳定错误码、日志字段和前端可识别的 retryable 语义。',
    '每个阶段先写测试，再实现代码；完成后运行后端测试、前端类型检查和构建。',
]: add_bullet(doc, t)

h = doc.add_heading('三 实现面试复盘报告', level=1)
doc.add_heading('3.1 目标与接口', level=2)
doc.add_paragraph('新增接口：')
add_code(doc, 'GET /api/interviews/{session_id}/report')
doc.add_paragraph('接口必须复用当前 _owned_session 或等价的会话归属校验。登录用户只能访问自己的会话，匿名用户只能访问自己的匿名会话。已知 session_id 不能绕过权限。')

doc.add_heading('3.2 建议文件', level=2)
add_code(doc, 'backend/app/services/reporting.py\nbackend/app/schemas/report.py\n修改 backend/app/api/interviews.py\n必要时修改 answers.py、questions.py')
doc.add_paragraph('如果项目已经存在报告模块，优先扩展现有模块。报告计算应保持纯函数化，输入数据库记录，输出可序列化的报告对象，便于单元测试。')

doc.add_heading('3.3 统计口径', level=2)
add_table(doc, ['统计项', '规则'], [
    ('有效评分', '只统计 processing_status == COMPLETED 且存在有效 score 的记录'),
    ('跳过题', '不参与平均分，但计入 skipped_count'),
    ('失败和未完成', '不参与评分，分别计入 failed_count 和 incomplete_count'),
    ('总体分数', '有效 score 的算术平均，四舍五入；没有有效数据时返回 null'),
    ('评分覆盖率', '有效评分题数 / 非跳过题总数；分母为 0 时返回 0'),
    ('追问统计', '统计 kind == follow_up 的已完成记录数量'),
], [1.35, 5.55])

doc.add_heading('3.4 返回结构', level=2)
add_code(doc, '''{
  "session_id": "...",
  "status": "FINISHED",
  "is_final": true,
  "generated_at": "2026-01-01T00:00:00Z",
  "summary": {
    "question_count": 10,
    "completed_count": 8,
    "skipped_count": 1,
    "failed_count": 1,
    "overall_score": 82,
    "score_coverage": 0.8,
    "followup_count": 3
  },
  "dimension_scores": {},
  "category_summary": [],
  "weak_points": [],
  "evidence": [],
  "suggestions": []
}''')

doc.add_heading('3.5 五维能力与类别统计', level=2)
doc.add_paragraph('从 evaluation.dimension_scores 聚合 correctness、completeness、logic、depth、clarity。每个维度只统计存在有效数值的记录，并返回 score 和 sample_count。根据 InterviewQuestion.category 聚合题目类别；空类别统一为“未分类”。')

doc.add_heading('3.6 薄弱点、证据与建议', level=2)
for t in [
    '薄弱点从 missing_points 提取，去除空字符串，完全相同内容合并，按出现次数倒序，最多返回 10 条，并附 question_ids。',
    '证据选择最高分和最低分记录，最多 6 条。返回题目、回答摘要、分数和反馈；回答摘要最多 500 字符。',
    '不要把 rubric_results 原样返回。它包含内部评分要点，可能造成泄题。',
    '根据五维平均分生成程序化改进建议，不新增模型调用；同一建议去重。',
    '没有评分时返回 overall_score: null，并给出“没有足够有效评分数据”的提示，绝不能用 0 分代替。',
]: add_bullet(doc, t)

doc.add_heading('3.7 报告测试', level=2)
for t in ['全量完成、部分完成、含跳过、含失败、没有评分数据。', '五维分数部分为空、类别为空、重复 missing_points 合并。', '跨账号访问被拒绝。', '报告生成过程不调用模型，可使用 mock 断言模型调用次数为 0。']: add_bullet(doc, t)

h = doc.add_heading('四 引入数据库级 write_epoch', level=1)
doc.add_heading('4.1 目标与字段', level=2)
doc.add_paragraph('在 interview_sessions 增加数据库级写入代次，防止旧 Worker 在 Redis 锁失效后仍然写入旧结果。')
add_code(doc, 'write_epoch BIGINT NOT NULL DEFAULT 0')
doc.add_paragraph('新增 Alembic migration。若回答表需要记录执行代次，可增加 answer.write_epoch；但必须区分它与 expected_state_version、result_state_version 的语义。')

doc.add_heading('4.2 两个版本字段的职责', level=2)
add_table(doc, ['字段', '职责'], [('write_epoch', '判断当前请求是否仍具有写入资格，锁接管后旧请求失效'), ('state_version', '判断业务状态是否仍是当前请求预期的状态，防止状态覆盖')], [1.55, 5.35])

doc.add_heading('4.3 获取和使用流程', level=2)
add_code(doc, '''拿 Redis 会话锁
→ 数据库事务内 SELECT 会话 FOR UPDATE
→ write_epoch 递增并取得 request_epoch
→ 读取当前 state_version
→ 释放数据库行锁，执行模型调用
→ 写入前检查 lock.lost
→ 使用 write_epoch + state_version 条件更新
→ 成功后递增 state_version 并提交''')
doc.add_paragraph('模型调用期间不能持有数据库行锁或长事务。获取 request_epoch 的事务应尽快提交，避免把数据库锁的生命周期扩大到模型调用。')

doc.add_heading('4.4 写入保护范围', level=2)
for t in ['会话状态、当前题目、进度、完成标记和 state_version。', '回答的 PENDING、PROCESSING、COMPLETED、FAILED 状态。', '评分、规则裁决、响应快照、错误码和重试字段。', '动态追问题目落库。', 'finish_interview、recover_interview 和 Checkpoint 丢失后的重建。']: add_bullet(doc, t)
doc.add_paragraph('不能只修改 bump_state_version。所有关键写入都应通过统一的 fenced update 或 assert_write_fence 封装。条件更新影响行数为 0 时，必须回滚并返回 WRITE_FENCE_LOST 或 VERSION_CONFLICT。')

doc.add_heading('4.5 并发测试', level=2)
for t in ['A 锁过期，B 获取新锁并推进，A 迟到返回后写入被拒绝。', 'A、B 同时提交同一会话，不能产生两份有效评分。', '旧请求尝试写评分、写进度、写追问、结束会话，均不能覆盖新状态。', 'Redis 重启或续租失败后，旧请求只能失败并进入可恢复路径。']: add_bullet(doc, t)

h = doc.add_heading('五 完善失败提交后台补偿', level=1)
doc.add_heading('5.1 数据字段和索引', level=2)
doc.add_paragraph('先检查 InterviewAnswer 是否已有同义字段，能复用则不重复添加。建议补充：')
add_code(doc, '''retryable BOOLEAN NOT NULL DEFAULT FALSE
next_retry_at TIMESTAMP NULL
attempt_count INTEGER NOT NULL DEFAULT 0
claimed_at TIMESTAMP NULL
claim_token VARCHAR(64) NULL''')
doc.add_paragraph('增加索引：(processing_status, retryable, next_retry_at)。claim_token 使用随机值，不能使用 session_id 或固定 Worker 名。')

doc.add_heading('5.2 错误分类', level=2)
add_table(doc, ['类型', '示例', '处理'], [
    ('可重试', 'MODEL_TIMEOUT、MODEL_UNAVAILABLE、有限次数的 MODEL_OUTPUT_INVALID', '保留回答，写 FAILED，设置 retryable 和 next_retry_at'),
    ('需恢复', 'STATE_RECOVERY_REQUIRED', '先恢复会话或重新对账，不能盲目重复调用'),
    ('不可重试', 'INVALID_REQUEST、UNREADABLE_MATERIAL、MATERIAL_INSUFFICIENT', 'retryable=false，保留失败原因'),
    ('业务冲突', 'REQUEST_CONFLICT、STALE_QUESTION', '不进入自动重试，返回明确错误'),
], [1.0, 2.4, 3.5])

doc.add_heading('5.3 任务抢占 SQL', level=2)
add_code(doc, '''UPDATE interview_answers
SET processing_status = 'PROCESSING',
    claimed_at = NOW(),
    claim_token = :claim_token,
    attempt_count = attempt_count + 1
WHERE id = :answer_id
  AND processing_status = 'FAILED'
  AND retryable = TRUE
  AND next_retry_at <= NOW()
  AND (claimed_at IS NULL OR claimed_at < NOW() - INTERVAL '5 minutes');''')
doc.add_paragraph('只有 rowcount == 1 的 Worker 可以继续处理。抢占失败说明任务已被其他 Worker 或用户重试接管，应直接跳过。')

doc.add_heading('5.4 Worker 与退避', level=2)
doc.add_paragraph('建议独立启动 backend-retry-worker，不要让每个 Uvicorn Worker 各自创建扫描线程。短期过渡实现也必须使用数据库抢占、每轮上限和固定扫描间隔。')
add_code(doc, '''第 1 次失败：30 秒后
第 2 次失败：2 分钟后
第 3 次失败：10 分钟后
第 4 次失败：30 分钟后
达到最大次数：retryable = FALSE''')
doc.add_paragraph('重试必须使用原 session_id、question_id、request_id 和回答内容。开始前重新检查会话归属、会话是否结束、回答是否已经 COMPLETED，以及是否已有其他执行者。')

doc.add_heading('5.5 与手动重试的冲突处理', level=2)
for t in ['两者都必须获取会话锁并重新读取数据库状态。', 'COMPLETED 直接回放，不再调用模型。', 'PROCESSING 且 claim 未过期时返回处理中。', 'FAILED 时只能有一个执行者通过条件更新抢占。', 'Worker 崩溃后，超过 claimed_at 超时时间的任务允许被再次接管。']: add_bullet(doc, t)

h = doc.add_heading('六 建立分层模型降级策略', level=1)
doc.add_heading('6.1 总体原则', level=2)
doc.add_paragraph('模型失败不能统一处理。解析、追问、评分的业务重要性不同：解析失败会阻止准备，追问失败可以跳过，评分失败必须保留回答但不能伪造分数或推进下一题。')

doc.add_heading('6.2 解析与结构化输出', level=2)
add_code(doc, '''正常模型输出
→ Pydantic 校验
→ 最多一次结构化修复
→ 修复仍失败：MODEL_OUTPUT_INVALID / MODEL_REPAIR_FAILED
→ 进入失败状态并提供可重试入口''')
doc.add_paragraph('修复请求只携带上一次输出和校验错误，不重复发送完整简历和全部图片。不得因为 JSON 错误生成空题目、0 分评价或伪造材料。')

doc.add_heading('6.3 追问失败', level=2)
add_code(doc, '''追问模型失败
→ 记录 FOLLOWUP_FAILED
→ rule_decision.decision = stop
→ 不生成追问题
→ 进入下一道主问题''')
doc.add_paragraph('追问是可选环节，因此允许跳过，但必须记录原因，不能重新询问已经回答过的主问题，不能让流程卡死。')

doc.add_heading('6.4 评分失败', level=2)
add_code(doc, '''评分失败
→ 保留用户原始回答
→ answer.processing_status = FAILED
→ 保存 EVALUATION_FAILED 等错误码
→ retryable = true（仅当错误可恢复）
→ 不生成 score / evaluation
→ 不推进下一题''')
doc.add_paragraph('用户重试时复用原 request_id 和原始回答。没有备用模型时，不要声称已实现多模型故障转移；可以采用一次结构化修复加有限重试，之后交给后台或用户重试。')

doc.add_heading('6.5 降级记录', level=2)
doc.add_paragraph('建议在回答表或独立模型调用表记录 degradation_level、degradation_reason、model_name、prompt_version、attempt_count、started_at、finished_at，便于分析模型质量和故障率。')

h = doc.add_heading('七 统一状态流转', level=1)
add_code(doc, '''PENDING → PROCESSING → COMPLETED
                 ↘ FAILED + retryable=true → PROCESSING
                 ↘ FAILED + retryable=false

追问失败：FOLLOWUP_FAILED → 下一道主问题
评分失败：EVALUATION_FAILED → 保留回答，不推进下一题''')
doc.add_paragraph('必须严格区分“追问失败”和“评分失败”：前者是可选体验降级，后者是核心结果缺失。')

h = doc.add_heading('八 验收标准与实施顺序', level=1)
doc.add_heading('8.1 验收标准', level=2)
for t in [
    '报告接口返回总体评分、五维能力、类别统计、薄弱点和回答证据，不调用模型，不泄露 rubric。',
    '旧 Worker 的迟到写入被 write_epoch 和 state_version 同时拒绝。',
    '失败任务可被后台扫描，多个 Worker 不重复抢占，后台与手动重试不重复执行。',
    '重试使用原 request_id，有最大次数和指数退避，不可重试错误不会进入队列。',
    '解析最多有限修复，追问失败可跳过，评分失败保留回答但不生成分数、不推进下一题。',
]: add_bullet(doc, t)

doc.add_heading('8.2 推荐顺序', level=2)
steps = ['实现 reporting 服务、schema 和 report 接口。', '补充报告统计、权限和“不调用模型”测试。', '增加 write_epoch migration，改造会话和关键写路径。', '增加旧 Worker 迟到写入及并发测试。', '增加失败任务字段、索引和后台 Worker。', '统一手动重试与后台重试的抢占逻辑。', '统一模型错误码、降级记录和状态流转。', '补充模型超时、无效 JSON、评分失败、追问失败测试。', '运行后端测试、前端 npx tsc --noEmit 和 npm run build。'],
for i, step in enumerate(steps, 1):
    p = doc.add_paragraph(style='List Number')
    p.paragraph_format.space_after = Pt(3)
    p.add_run(step)

h = doc.add_heading('九 交付时必须报告的内容', level=1)
for t in ['修改了哪些文件和数据库迁移。', '新增了哪些接口和返回字段。', '哪些错误可自动重试，哪些错误必须人工处理。', 'write_epoch 覆盖了哪些写入路径。', '后台 Worker 如何启动、如何抢占任务、如何退避。', '哪些恢复场景已通过测试，哪些仍有限制。', '测试是否真实通过；如果依赖 PostgreSQL 或 Redis，必须说明服务是否运行。']: add_bullet(doc, t)

# Footer
for section in doc.sections:
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rr = footer.add_run('AI面试系统四项优化实现方案')
    rr.font.size = Pt(8)
    rr.font.color.rgb = RGBColor(128, 128, 128)

# core properties
props = doc.core_properties
props.title = 'AI面试系统四项优化实现方案'
props.subject = '面向其他模型的工程实现说明'
props.author = 'Codex'

doc.save(OUT)
print(OUT)
