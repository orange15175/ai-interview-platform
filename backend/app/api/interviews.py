"""面试接口：把前端动作翻译成可恢复的业务操作。

阅读顺序：先看 create_interview，再看 get_current_question、submit_answer、
finish_interview。下划线方法是接口内部步骤：_read_upload 负责安全读取文件，
_run_preparation 负责后台准备，_set_stage 写前端进度，_fail_preparation 把
异常收口成失败状态。已有会话的访问会在接口或业务服务内查询归属，确认当前身份有权访问。

下面保留接口清单，方便从前端请求反查入口：

接口清单与职责边界：
- POST   /api/interviews                        multipart 上传材料 → 202 + session_id
- GET    /api/interviews/{id}                   查询进度；必要时标记准备任务中断，不推进题号
- GET    /api/interviews/{id}/current-question   当前题；checkpoint 丢失时自动触发恢复
- POST   /api/interviews/{id}/answers            提交回答/跳过（幂等编排见 interview_workflow）
- POST   /api/interviews/{id}/finish             用户提前结束（幂等）
- GET    /api/interviews/{id}/records            正式问答记录（不生成报告）
- GET    /api/interviews/{id}/report             复盘报告（只读聚合，不调用模型）
- POST   /api/interviews/{id}/retry-preparation  复用已存材料只重跑出题
- POST   /api/interviews/{id}/recover            显式从 PostgreSQL 恢复会话

目前的实现边界：
1. 准备任务是**进程内后台任务**：服务重启会让 PREPARING 会话变成孤儿。
   这里不做持久任务队列，靠"轮询时发现孤儿 → 标记 PREPARATION_FAILED"收口。
2. 归属校验全部在仓储层 SQL 里做（get_owned），查不到一律 404，
   不给探测者区分"不存在"与"不属于你"的机会。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import (
    get_current_user,
    get_db,
    get_graph,
    get_redis_client,
    get_visitor,
    require_same_origin,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.models import AnonymousVisitor, User
from app.parsers.files import validate_upload
from app.parsers.pipeline import build_parsed_input, validate_jd_text
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.schemas.report import InterviewReport
from app.services import graph_runtime, interview_workflow, locks, reporting
from app.services.recovery import recover_session

log = get_logger(__name__)

router = APIRouter(prefix="/interviews", tags=["interviews"])

# 进程内"正在准备"登记表。只用于孤儿检测，不承担跨进程语义。
_preparing: set[str] = set()

# 本进程启动时刻；当前按单 worker 假设，把更早创建且未登记的准备任务当作孤儿。
# 多 worker 或滚动启动时该判断可能误判，不能把它当作跨进程任务状态。
_PROCESS_STARTED_AT = datetime.now(timezone.utc)

# 本进程创建、但登记表里没有的 PREPARING 会话，要等这么久才判孤儿。
# 对本进程启动后创建的会话，多等一段时间再判失败，减少误判；它不是可靠的任务心跳。
_PREPARATION_TIMEOUT_SECONDS = 180

MAX_QUESTION_COUNT = 30  # 出题数上限（防费用失控，§11）
# "重试出题"的次数上限：材料已存，出题失败最多再试 3 次，之后要求重新上传
MAX_PREPARATION_RETRIES = 3

# 准备阶段的稳定标识（前后端共用）：前端按它显示中文名与耗时
PREPARATION_STAGES = (
    "QUEUED",              # 已受理，等待后台任务开始
    "VALIDATING_UPLOAD",   # 二次校验文件（真正能否打开）
    "PARSING_MATERIAL",    # 解析简历/JD（视觉模型）
    "VALIDATING_MATERIAL", # 解析结果的语义底线检查
    "GENERATING_QUESTIONS",# 出题（文字模型）
    "VALIDATING_QUESTIONS",# 题数/编号/rubric 校验
    "PERSISTING_QUESTIONS",# 题目落库 + 启动图
    "WAITING_ANSWER",      # 停在第一题，等作答
    "PREPARATION_FAILED",  # 失败终态
)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """把时间统一成带时区的 UTC，避免测试库与 PostgreSQL 的时间无法比较。

    PG 的 timestamptz 读回来已带时区；SQLite（测试）不存时区，读回来是 naive，
    而写入时用的就是 UTC，所以无时区按 UTC 解释是安全的。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _is_orphan_preparing(session, session_id: str) -> bool:
    """根据本进程登记表、启动时间和等待时长，判断准备任务是否可能已中断。"""
    if session_id in _preparing:
        return False  # 本进程正在跑，绝不误杀

    created = _as_aware_utc(session.created_at)
    if created is None:
        return True  # 连时间都没有：数据异常，按孤儿收口，别让它永远 PREPARING

    if created < _PROCESS_STARTED_AT:
        return True  # 单 worker 假设下按旧进程中断处理；多进程时未必成立

    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age > _PREPARATION_TIMEOUT_SECONDS


# ---------------------------------------------------------------- 请求模型


class AnswerIn(BaseModel):
    """提交回答的请求体。服务端不接受分数、评分要点或任何 Graph State（§10）。"""

    question_id: uuid.UUID
    request_id: str = Field(min_length=1, max_length=64)
    answer: str | None = None
    submission_kind: Literal["answer", "skip"] | None = None
    expected_state_version: int | None = None

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, v: str | None) -> str | None:
        """回答字段如果提供就不能全是空白；不作答应明确提交 skip。"""
        if v is not None and not v.strip():
            raise ValueError("回答不能为空；如不想作答请用 submission_kind=skip")
        return v


# ---------------------------------------------------------------- 内部工具


def _owned_session(
    db: OrmSession,
    session_id: str,
    visitor: AnonymousVisitor,
    user: User | None = None,
):
    """按当前身份查询会话：登录时认账号，匿名时只认本访客未绑定账号的会话。"""
    return sessions_repo.get_owned(
        db, session_id, visitor.id, user_id=user.id if user else None
    )


def _validate_question_count(count: int | None) -> int | None:
    """把用户指定题数限制在 1～30；未指定时留给配置决定默认值。"""
    if count is None:
        return None
    if not 1 <= count <= MAX_QUESTION_COUNT:
        raise AppError(
            f"题目数量必须在 1~{MAX_QUESTION_COUNT} 之间",
            code="INVALID_REQUEST", http_status=422,
        )
    return count


async def _read_upload(
    file: UploadFile | None, *, limit_mb: int, label: str
) -> tuple[str | None, bytes | None]:
    """分块读取已经由框架解析的上传文件，超过大小限制就停止继续读。"""
    if file is None:
        return None, None

    limit = limit_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(256 * 1024)  # 每片 256 KiB
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise AppError(
                f"{label}超过 {limit_mb} MiB 上限，请压缩后再上传",
                code="FILE_TOO_LARGE",
                http_status=422,
            )
        chunks.append(chunk)

    if total == 0:
        return None, None
    return file.filename or "", b"".join(chunks)


def _quick_validate(filename: str | None, data: bytes | None, label: str) -> None:
    """在受理前检查文件大小、格式和基本可读性，尽早把错误返回前端。"""
    if filename is None:
        return
    try:
        validate_upload(filename, data or b"", get_settings().business)
    except AppError as exc:
        raise AppError(f"{label}：{exc.message}", code=exc.code, http_status=exc.http_status) from exc


# ---------------------------------------------------------------- 准备任务


def _set_stage(db: OrmSession, session, stage: str, **extra) -> None:
    """保存准备阶段和开始时间，让前端知道当前在解析、出题还是保存题目。"""
    now = datetime.now(timezone.utc).isoformat()
    previous = session.progress or {}
    carried = {
        k: previous[k]
        for k in ("retry_count", "text_blocks", "render_blocks", "char_count", "warnings")
        if previous.get(k) is not None
    }
    progress = {
        **carried,
        **{k: v for k, v in extra.items() if v is not None},
        "stage": stage,
        "stage_started_at": now,
        "updated_at": now,
        "question_count": extra.get("question_count") or session.question_count,
    }
    sessions_repo.touch(db, session, progress=progress)
    db.commit()


def _fail_preparation(db: OrmSession, session_id: str, exc: AppError | None,
                      stage: str) -> None:
    """回滚当前事务，再把失败阶段和错误写回会话，供前端提示原因。"""
    db.rollback()
    session = sessions_repo.get(db, session_id)
    if session is None:
        return
    sessions_repo.mark_preparation_failed(
        db, session,
        exc.message if exc is not None else "准备过程发生未预期的错误，请重试或重新上传",
        stage=stage,
        error_code=exc.code if exc is not None else "INTERNAL_ERROR",
    )
    db.commit()


def _run_questions_only(*, session_id: str) -> None:
    """读取已保存材料重新出题，省去再次上传和解析简历。

    出题失败时材料（往往已经花过视觉模型的钱）已经落库，重传一份简历等于
    让用户为同一个错误付第二次钱（§十）。"""
    from app.infrastructure.db import get_session_factory
    from app.repositories import materials as materials_repo

    db = get_session_factory()()
    stage = "GENERATING_QUESTIONS"
    try:
        session = sessions_repo.get(db, session_id)
        if session is None or session.status != "PREPARING":
            return

        parsed = materials_repo.load_parsed(db, session.id)
        if parsed is None:
            _fail_preparation(
                db, session_id,
                AppError("材料没有保存成功，请重新上传简历", code="MATERIAL_MISSING",
                         http_status=409),
                "PARSING_MATERIAL",
            )
            return

        _set_stage(db, session, "GENERATING_QUESTIONS")
        from app.agents.question_agent import generate_questions

        questions = generate_questions(parsed, session.question_count)

        stage = "PERSISTING_QUESTIONS"
        _set_stage(db, session, stage, question_count=len(questions.questions))
        interview_workflow.start_interview(
            db=db, graph=graph_runtime.get_compiled_graph(),
            session=session, questions=questions,
        )
        log.info("会话 %s 重试出题成功（复用已存材料）", session_id)
    except AppError as exc:
        _fail_preparation(db, session_id, exc, stage)
        log.warning("会话 %s 重试出题失败 code=%s: %s", session_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        _fail_preparation(db, session_id, None, stage)
        log.exception("会话 %s 重试出题异常: %s", session_id, type(exc).__name__)
    finally:
        _preparing.discard(session_id)
        db.close()


def _run_preparation(
    *,
    session_id: str,
    resume_filename: str,
    resume_bytes: bytes,
    jd_filename: str | None,
    jd_bytes: bytes | None,
    jd_text: str | None,
) -> None:
    """后台依次提取文件、调用解析模型、保存材料、生成题目并启动答题图。

    全程自开数据库会话（后台任务拿不到请求级会话）。
    每个阶段都写进 session.progress（stage + stage_started_at），前端能显示
    "解析中/出题中/保存题目"，而不是一句含糊的"准备中"。
    任何失败都收口成 PREPARATION_FAILED（终态 + 脱敏错误 + 失败阶段）。"""
    from app.infrastructure.db import get_session_factory

    db = get_session_factory()()
    stage = "VALIDATING_UPLOAD"
    try:
        session = sessions_repo.get(db, session_id)
        if session is None or session.status != "PREPARING":
            log.warning("准备任务被跳过 session=%s status=%s", session_id,
                        session.status if session else "missing")
            return

        _set_stage(db, session, "VALIDATING_UPLOAD")
        parsed_input = build_parsed_input(
            resume_filename=resume_filename,
            resume_bytes=resume_bytes,
            jd_filename=jd_filename,
            jd_bytes=jd_bytes,
            jd_text=jd_text,
        )

        stage = "PARSING_MATERIAL"
        from app.parsers.pipeline import describe_input

        _set_stage(db, session, stage, **describe_input(parsed_input))

        from app.agents.parse_agent import parse_materials

        parsed = parse_materials(parsed_input)

        stage = "VALIDATING_MATERIAL"
        _set_stage(db, session, stage, warnings=len(parsed.warnings))

        from app.repositories import materials as materials_repo

        materials_repo.save_parsed(
            db, session_id=session.id, parsed=parsed,
            parse_model=get_settings().vision_model.model,
        )
        db.commit()

        stage = "GENERATING_QUESTIONS"
        _set_stage(db, session, stage)

        from app.agents.question_agent import generate_questions

        questions = generate_questions(parsed, session.question_count)

        stage = "PERSISTING_QUESTIONS"
        _set_stage(db, session, stage, question_count=len(questions.questions))
        interview_workflow.start_interview(
            db=db, graph=graph_runtime.get_compiled_graph(),
            session=session, questions=questions,
        )
        log.info("会话 %s 准备完成，进入答题", session_id)
    except AppError as exc:
        _fail_preparation(db, session_id, exc, stage)
        log.warning("会话 %s 准备失败 stage=%s code=%s: %s",
                    session_id, stage, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 —— 后台任务没有调用方接异常，必须自己收口
        _fail_preparation(db, session_id, None, stage)
        log.exception("会话 %s 准备异常: %s", session_id, type(exc).__name__)
    finally:
        _preparing.discard(session_id)
        db.close()


# ---------------------------------------------------------------- 创建会话


class SessionListItem(BaseModel):
    """列表页一行：够渲染一张卡片，不含题干/作答原文。"""

    session_id: str
    status: str
    question_count: int
    answered_count: int
    # 没有有效评分时为 null（不是 0，理由同报告）
    overall_score: int | None = None
    created_at: datetime
    last_activity_at: datetime
    # 已终结的会话不再能答题，但报告仍可看
    finished: bool
    # 能直接接着答（还在准备或答题中）
    resumable: bool


@router.get("", response_model=list[SessionListItem], summary="面试记录列表（只读）")
def list_interviews(
    limit: int = 20,
    offset: int = 0,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> list[SessionListItem]:
    """按当前身份返回最近的面试会话，最近的排前面。

    归属规则与单会话读取完全一致（sessions_repo 里共用同一个 where 条件）：
    登录态按账号，匿名态按访客 Cookie 且排除已被账号认领的会话。
    """
    sessions = sessions_repo.list_owned(
        db, visitor.id, user_id=user.id if user else None, limit=limit, offset=offset
    )
    if not sessions:
        return []

    # 一次算出这一页所有会话的平均分，避免每行单独聚合（N+1）
    averages = answers_repo.average_scores(db, [s.id for s in sessions])

    items: list[SessionListItem] = []
    for s in sessions:
        avg = averages.get(s.id)
        items.append(
            SessionListItem(
                session_id=str(s.id),
                status=s.status,
                question_count=s.question_count,
                answered_count=int((s.progress or {}).get("answered_count") or 0),
                overall_score=int(round(avg)) if avg is not None else None,
                created_at=s.created_at,
                last_activity_at=s.last_activity_at,
                finished=s.status in sessions_repo.TERMINAL_STATUSES,
                resumable=s.status in ("PREPARING", "IN_PROGRESS"),
            )
        )
    return items


@router.post(
    "",
    status_code=202,
    summary="创建面试会话并上传材料",
    dependencies=[Depends(require_same_origin)],
)
async def create_interview(
    background_tasks: BackgroundTasks,
    resume: UploadFile = File(..., description="简历（docx/pdf/图片，≤10MiB）"),
    jd: UploadFile | None = File(None, description="岗位描述文件（可选，与 jd_text 互斥）"),
    jd_text: str | None = Form(
        None,
        description=(
            "岗位描述文字（可选，与 jd 文件互斥）。"
            "留空或只填空格视为未提供；填写则至少 10 个字，"
            "不足会在返回 202 之前直接 422（不会先受理再后台失败）。"
        ),
    ),
    question_count: int | None = Form(None, description="题目数量（默认 15）"),
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """接收上传材料，校验后建会话并登记后台任务，返回 202 让前端轮询进度。

    这是进程内后台任务：服务重启会中断准备，
    孤儿会话在轮询时被标记 PREPARATION_FAILED，需要重新上传。"""
    question_count = _validate_question_count(question_count)

    limits = get_settings().business
    resume_name, resume_data = await _read_upload(
        resume, limit_mb=limits.max_file_mb, label="简历"
    )
    jd_name, jd_data = await _read_upload(jd, limit_mb=limits.max_file_mb, label="岗位描述")

    if resume_name is None:
        raise AppError("请上传简历文件", code="INVALID_FILE", http_status=422)
    if jd_data is not None and jd_text and jd_text.strip():
        raise AppError(
            "JD 请只提供一种形式：文件或文字，不要同时上传",
            code="INVALID_FILE", http_status=422,
        )

    # JD 文字长度在 202 之前校验：放到后台做，用户要等半分钟才知道填短了（§八）
    jd_text = validate_jd_text(jd_text)

    # 快速校验放在 202 之前：用户立刻知道文件合不合规，不用轮询才知道
    _quick_validate(resume_name, resume_data, "简历")
    if jd_name is not None:
        _quick_validate(jd_name, jd_data, "岗位描述")
    session = sessions_repo.create(
        db,
        owner_id=visitor.id,
        question_count=question_count,
        user_id=user.id if user else None,
    )
    _set_stage(
        db, session, "QUEUED",
        retry_count=0,
        resume_size=len(resume_data or b""),
        jd_size=len(jd_data or b""),
        jd_text_chars=len(jd_text or ""),
    )

    sid = str(session.id)
    _preparing.add(sid)
    background_tasks.add_task(
        _run_preparation,
        session_id=sid,
        resume_filename=resume_name,
        resume_bytes=resume_data or b"",
        jd_filename=jd_name,
        jd_bytes=jd_data,
        jd_text=jd_text,
    )
    log.info("访客 %s 创建会话 %s（%d 题）", visitor.id, sid, session.question_count)
    return {
        "session_id": sid,
        "status": session.status,
        "state_version": session.state_version,
    }


# ---------------------------------------------------------------- 重试准备


@router.post(
    "/{session_id}/retry-preparation",
    status_code=202,
    summary="解析成功但出题失败：复用已存材料只重跑出题",
    dependencies=[Depends(require_same_origin)],
)
def retry_preparation(
    session_id: str,
    background_tasks: BackgroundTasks,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """确认准备失败且材料已保存后，增加重试次数并再次登记出题任务。

    前提是解析结果已经落库；没落库说明失败发生在解析阶段，只能重新上传。
    重试次数有限（每次失败都会记录阶段），避免无限重试把费用打上去。"""
    from app.repositories import materials as materials_repo

    session = _owned_session(db, session_id, visitor, user)
    if session.status != "PREPARATION_FAILED":
        raise AppError(
            "只有准备失败的面试可以重试出题",
            code="INVALID_STATE", http_status=409,
        )

    materials = materials_repo.get(db, session.id)
    if materials is None:
        raise AppError(
            "材料没有保存成功（失败发生在解析阶段），请重新上传简历",
            code="MATERIAL_MISSING", http_status=409,
        )

    retries = int((session.progress or {}).get("retry_count") or 0)
    if retries >= MAX_PREPARATION_RETRIES:
        raise AppError(
            f"已重试 {retries} 次仍未成功，请重新上传简历",
            code="RETRY_LIMIT_REACHED", http_status=409,
        )

    # 当前先读后写，没有原子领取任务：并发点击可能同时通过上面的检查。
    # 阅读时不要把 _preparing 集合误认为这里已经取得了分布式锁。
    _set_stage(db, session, "QUEUED", retry_count=retries + 1, attempt=retries + 1)
    sessions_repo.touch(
        db, session,
        status="PREPARING",
        completion_reason=None,
        sanitized_error=None,
        finished=False,
    )
    db.commit()

    _preparing.add(session_id)
    background_tasks.add_task(_run_questions_only, session_id=session_id)
    log.info("会话 %s 触发重试出题（第 %d 次）", session_id, retries + 1)
    return {
        "session_id": session_id,
        "status": "PREPARING",
        "retry_count": retries + 1,
        "state_version": session.state_version,
    }


# ---------------------------------------------------------------- 查询类

@router.get("/{session_id}", summary="查询进度与可恢复性（只读）")
def get_interview(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
) -> dict:
    """返回会话进度；发现准备任务疑似中断时会把它写成准备失败。"""
    session = _owned_session(db, session_id, visitor, user)

    if session.status == "PREPARING" and _is_orphan_preparing(session, session_id):
        # 进程重启/任务丢失导致准备任务孤儿化：如实标记失败，让用户重新上传（§10）
        sessions_repo.mark_preparation_failed(
            db, session,
            "准备过程被服务重启或中断，请重新上传材料；"
            "若简历已经解析成功，也可直接点「重试出题」继续",
            stage=(session.progress or {}).get("stage"),
            error_code="PREPARATION_INTERRUPTED",
        )
        db.commit()
        db.refresh(session)

    if session.status == "PREPARING":
        return {
            "session_id": str(session.id),
            "status": "PREPARING",
            "state_version": session.state_version,
            "question_count": session.question_count,
            "finished": False,
            "recoverable": False,
            "progress": session.progress or {},
        }

    view = interview_workflow.build_progress_view(db=db, graph=graph, session=session)
    if session.sanitized_error:
        view["sanitized_error"] = session.sanitized_error
    return view


@router.post(
    "/{session_id}/recover",
    summary="显式从记录恢复（状态不一致时用）",
    dependencies=[Depends(require_same_origin)],
)
def recover_interview(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """先确认会话归属，再调用恢复服务，从数据库记录重建答题位置。

    什么时候需要它：提交回答返回 `STATE_RECOVERY_REQUIRED`（图位置与记录对不上），
    或者 Redis 里的执行状态丢了。恢复会拿会话锁、对账未完成提交，并对
    已有评分的回答补齐裁决与快照——不会重新评分，也不会越过当前题。"""
    session = _owned_session(db, session_id, visitor, user)
    result = recover_session(
        db=db,
        restore_graph=graph_runtime.get_compiled_graph(restore=True),
        session=session,
    )
    return result


@router.get("/{session_id}/current-question", summary="获取当前题（不推进）")
def get_current_question(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
) -> dict:
    """返回数据库记录的当前题；Redis 执行状态丢失时先自动恢复。"""
    session = _owned_session(db, session_id, visitor, user)

    if session.status == "PREPARING":
        raise AppError(
            "简历还在准备中，请稍候", code="PREPARING", http_status=409, retryable=True
        )

    if session.status in sessions_repo.TERMINAL_STATUSES:
        return {
            "session_id": str(session.id),
            "status": session.status,
            "question": None,
            "finished": True,
            "state_version": session.state_version,
        }

    if graph_runtime.has_checkpoint(graph, session.id):
        question = None
        if session.current_question_id is not None:
            row = questions_repo.get(db, session.current_question_id)
            if row is not None:
                question = questions_repo.public_dict(row)
        return {
            "session_id": str(session.id),
            "status": session.status,
            "question": question,
            "finished": False,
            "state_version": session.state_version,
            "recovered": False,
        }

    # checkpoint 丢失：从 PostgreSQL 显式重建到安全等待点（§9.4）。
    # 恢复本身拿锁、对账未完成提交，前端"刷新"即可回到当前题。
    result = recover_session(
        db=db,
        restore_graph=graph_runtime.get_compiled_graph(restore=True),
        session=session,
    )
    return {
        "session_id": result["session_id"],
        "status": result["status"],
        "question": result["question"],
        "finished": result["finished"],
        "state_version": result["state_version"],
        "recovered": True,
    }


# ---------------------------------------------------------------- 写操作


@router.post(
    "/{session_id}/answers",
    summary="提交回答或跳过",
    dependencies=[Depends(require_same_origin)],
)
def submit_answer(
    session_id: str,
    body: AnswerIn,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
    redis_client=Depends(get_redis_client),
) -> dict:
    """校验回答或跳过参数，再交给业务服务完成幂等判断、评分和题序推进。"""
    skipped = body.submission_kind == "skip"
    if not skipped and not (body.answer or "").strip():
        raise AppError(
            "回答不能为空；如不想作答请用 submission_kind=skip",
            code="INVALID_REQUEST", http_status=422,
        )
    if len(body.answer or "") > get_settings().business.answer_max_chars:
        raise AppError(
            f"回答过长（最多 {get_settings().business.answer_max_chars} 字）",
            code="INVALID_REQUEST", http_status=422,
        )

    return interview_workflow.submit_answer(
        db=db,
        graph=graph,
        session_id=session_id,
        owner_id=visitor.id,
        question_id=body.question_id,
        request_id=body.request_id,
        answer=None if skipped else body.answer,
        skipped=skipped,
        expected_state_version=body.expected_state_version,
        user_id=user.id if user else None,
        redis_client=redis_client,
    )


@router.post(
    "/{session_id}/finish",
    summary="用户提前结束（幂等）",
    dependencies=[Depends(require_same_origin)],
)
def finish_interview(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """拿会话锁后把面试标成用户提前结束；重复结束直接返回现有终态。"""
    session = _owned_session(db, session_id, visitor, user)

    if session.status == "PREPARING":
        # 准备还在进行：后台任务随后会把状态收口，这里不允许半路截断
        raise AppError(
            "简历还在准备中，请等准备完成后再结束",
            code="PREPARING", http_status=409, retryable=True,
        )
    if session.status in sessions_repo.TERMINAL_STATUSES:
        # 幂等：已结束的会话重复调用返回同一事实，不再变更
        return {
            "session_id": str(session.id),
            "status": session.status,
            "state_version": session.state_version,
            "finished": True,
        }

    lock = locks.acquire(session_id)
    lock.start_auto_renew()
    try:
        db.refresh(session)
        if session.status in sessions_repo.TERMINAL_STATUSES:
            return {
                "session_id": str(session.id),
                "status": session.status,
                "state_version": session.state_version,
                "finished": True,
            }
        # 提前结束也是一次会话写入：取得写入栅栏，别让一个锁已过期的旧请求
        # 把别人刚恢复的面试"结束"掉。
        fence = sessions_repo.begin_write(db, session)
        sessions_repo.check_fence(session, fence)
        sessions_repo.touch(
            db,
            session,
            status="CANCELLED",
            current_question_id=None,
            completion_reason="USER_ENDED",
            finished=True,
            progress={"stage": "FINISHED", "ended_by": "user"},
        )
        sessions_repo.bump_state_version(db, session, fence=fence)
        db.commit()
        log.info("会话 %s 被用户提前结束", session_id)
        return {
            "session_id": str(session.id),
            "status": session.status,
            "state_version": session.state_version,
            "finished": True,
        }
    finally:
        lock.release()


# ---------------------------------------------------------------- 正式记录


@router.get("/{session_id}/records", summary="查询正式问答记录（不生成报告）")
def get_records(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """按题序返回已完成的正式问答，过滤失败提交和内部评分要点。"""
    session = _owned_session(db, session_id, visitor, user)

    questions = questions_repo.list_for_session(db, session.id)
    by_question = {a.question_id: a for a in answers_repo.list_for_session(db, session.id)}

    records: list[dict] = []
    for q in sorted(questions, key=lambda x: (x.main_index, x.follow_up_index)):
        answer = by_question.get(q.id)
        if answer is None or answer.processing_status != "COMPLETED":
            continue  # 未完成/失败的提交不是正式记录
        records.append({
            "question_id": str(q.id),
            "kind": q.kind,
            "main_index": q.main_index,
            "follow_up_index": q.follow_up_index,
            "category": q.category,
            "content": q.content,
            "answer_id": str(answer.id),
            "skipped": answer.submission_kind == "skip",
            "answer_content": answer.answer_content,
            "score": answer.score,
            "evaluation": interview_workflow.public_evaluation(answer.evaluation),
            "followup_decision": interview_workflow.public_decision(answer.rule_decision),
        })

    return {
        "session_id": str(session.id),
        "status": session.status,
        "record_count": len(records),
        "records": records,
    }


# ---------------------------------------------------------------- 复盘报告


@router.get(
    "/{session_id}/report",
    response_model=InterviewReport,
    summary="面试复盘报告（只读，不调用模型）",
)
def get_report(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    user: User | None = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> InterviewReport:
    """读取题目和回答生成复盘报告；只聚合现有记录，不额外调用模型。

    三条边界：
    - **只读**：不调用任何模型，不改会话状态。同一份记录重复打开，结论一致。
    - **不泄题**：evaluation 里的 rubric_results（评分要点）一律不出现在响应里。
    - **不编数**：没有有效评分时 overall_score 为 null 并附说明，绝不用 0 分代替。

    会话还在答题中也允许看：此时报告只覆盖已完成的题目，notice 会写明。

    落库策略（cache-aside）：终结且每题都有终态结果的报告会存进
    interview_sessions.report_snapshot，之后直接读快照；否则每次现场聚合、
    不写库——中途的报告本来就该随新的答题变化。
    """
    session = _owned_session(db, session_id, visitor, user)
    if session.status == "PREPARING":
        raise AppError(
            "简历还在准备中，报告要等面试开始后才能生成",
            code="PREPARING", http_status=409, retryable=True,
        )

    cached = reporting.load_snapshot(session)
    if cached is not None:
        return cached

    report = reporting.build_report(
        session=session,
        questions=questions_repo.list_for_session(db, session.id),
        answers=answers_repo.list_for_session(db, session.id),
    )
    # 不可固化（还在答题中／有失败题）时内部直接跳过，这里不用分支。
    reporting.save_snapshot(db, session, report)
    return report
