"""面试 6 个 API（开发说明 §10），全部走匿名访客 Cookie 鉴权。

接口清单与职责边界：
- POST   /api/interviews                        multipart 上传材料 → 202 + session_id
- GET    /api/interviews/{id}                   准备进度/可恢复性（只读，不推进题号）
- GET    /api/interviews/{id}/current-question   当前题；checkpoint 丢失时自动触发恢复
- POST   /api/interviews/{id}/answers            提交回答/跳过（幂等编排见 interview_workflow）
- POST   /api/interviews/{id}/finish             用户提前结束（幂等）
- GET    /api/interviews/{id}/records            正式问答记录（不生成报告）

两条诚实声明（§10，不得向用户隐瞒）：
1. 准备任务是**进程内后台任务**：服务重启会让 PREPARING 会话变成孤儿。
   这里不做持久任务队列，靠"轮询时发现孤儿 → 标记 PREPARATION_FAILED"收口。
2. 归属校验全部在仓储层 SQL 里做（get_owned），查不到一律 404，
   不给探测者区分"不存在"与"不属于你"的机会。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import get_db, get_graph, get_redis_client, get_visitor, require_same_origin
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.models import AnonymousVisitor
from app.parsers.files import validate_upload
from app.parsers.pipeline import build_parsed_input
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.services import graph_runtime, interview_workflow, locks
from app.services.recovery import recover_session

log = get_logger(__name__)

router = APIRouter(prefix="/interviews", tags=["interviews"])

# 进程内"正在准备"登记表。只用于孤儿检测，不承担跨进程语义。
_preparing: set[str] = set()

MAX_QUESTION_COUNT = 30  # 出题数上限（防费用失控，§11）


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
        if v is not None and not v.strip():
            raise ValueError("回答不能为空；如不想作答请用 submission_kind=skip")
        return v


# ---------------------------------------------------------------- 内部工具


def _owned_session(
    db: OrmSession, session_id: str, visitor: AnonymousVisitor
):
    return sessions_repo.get_owned(db, session_id, visitor.id)


def _validate_question_count(count: int | None) -> int | None:
    if count is None:
        return None
    if not 1 <= count <= MAX_QUESTION_COUNT:
        raise AppError(
            f"题目数量必须在 1~{MAX_QUESTION_COUNT} 之间",
            code="INVALID_REQUEST", http_status=422,
        )
    return count


async def _read_upload(file: UploadFile | None) -> tuple[str | None, bytes | None]:
    """读上传文件字节。None 传 None；空文件视同未提供。"""
    if file is None:
        return None, None
    data = await file.read()
    if not data:
        return None, None
    return file.filename or "", data


def _quick_validate(filename: str | None, data: bytes | None, label: str) -> None:
    """在 202 之前做一遍快速校验（大小/扩展名/魔数），让用户当场拿到错误。

    完整的"能否真正打开"与解析在后台任务里做——那部分慢，不该阻塞响应。
    """
    if filename is None:
        return
    try:
        validate_upload(filename, data or b"", get_settings().business)
    except AppError as exc:
        raise AppError(f"{label}：{exc.message}", code=exc.code, http_status=exc.http_status) from exc


# ---------------------------------------------------------------- 准备任务


def _run_preparation(
    *,
    session_id: str,
    resume_filename: str,
    resume_bytes: bytes,
    jd_filename: str | None,
    jd_bytes: bytes | None,
    jd_text: str | None,
) -> None:
    """后台准备：解析 → 出题 → 启动图，停在第一题的等待点。

    全程自开数据库会话（后台任务拿不到请求级会话）。
    任何失败都收口成 PREPARATION_FAILED（终态 + 脱敏错误），绝不留一个
    "永远 PREPARING"的僵尸会话。
    """
    from app.infrastructure.db import get_session_factory

    db = get_session_factory()()
    try:
        session = sessions_repo.get(db, session_id)
        if session is None or session.status != "PREPARING":
            log.warning("准备任务被跳过 session=%s status=%s", session_id,
                        session.status if session else "missing")
            return

        parsed_input = build_parsed_input(
            resume_filename=resume_filename,
            resume_bytes=resume_bytes,
            jd_filename=jd_filename,
            jd_bytes=jd_bytes,
            jd_text=jd_text,
        )
        from app.agents.parse_agent import parse_materials

        parsed = parse_materials(parsed_input)

        from app.repositories import materials as materials_repo

        materials_repo.save_parsed(
            db, session_id=session.id, parsed=parsed,
            parse_model=get_settings().vision_model.model,
        )
        db.commit()

        from app.agents.question_agent import generate_questions

        questions = generate_questions(parsed, session.question_count)
        interview_workflow.start_interview(
            db=db, graph=graph_runtime.get_compiled_graph(),
            session=session, questions=questions,
        )
        log.info("会话 %s 准备完成，进入答题", session_id)
    except AppError as exc:
        db.rollback()
        session = sessions_repo.get(db, session_id)
        if session is not None:
            sessions_repo.mark_preparation_failed(db, session, exc.message)
            db.commit()
        log.warning("会话 %s 准备失败 code=%s: %s", session_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 —— 后台任务没有调用方接异常，必须自己收口
        db.rollback()
        session = sessions_repo.get(db, session_id)
        if session is not None:
            sessions_repo.mark_preparation_failed(
                db, session, "准备过程发生未预期的错误，请重试或重新上传"
            )
            db.commit()
        log.exception("会话 %s 准备异常: %s", session_id, type(exc).__name__)
    finally:
        _preparing.discard(session_id)
        db.close()


# ---------------------------------------------------------------- 创建会话


@router.post(
    "",
    status_code=202,
    summary="创建面试会话并上传材料",
    dependencies=[Depends(require_same_origin)],
)
async def create_interview(
    background_tasks: BackgroundTasks,
    resume: UploadFile = File(..., description="简历（docx/pdf/图片）"),
    jd: UploadFile | None = File(None, description="岗位描述文件（可选）"),
    jd_text: str | None = Form(None, description="岗位描述文字（可选，与 jd 文件互斥）"),
    question_count: int | None = Form(None, description="题目数量（默认 15）"),
    visitor: AnonymousVisitor = Depends(get_visitor),
    db: OrmSession = Depends(get_db),
) -> dict:
    """202 + session_id：创建较快，准备结果靠轮询 GET /api/interviews/{id}。

    这是进程内后台任务（§10 的明确边界）：服务重启会中断准备，
    孤儿会话在轮询时被标记 PREPARATION_FAILED，需要重新上传。
    """
    question_count = _validate_question_count(question_count)

    resume_name, resume_data = await _read_upload(resume)
    jd_name, jd_data = await _read_upload(jd)
    jd_text = jd_text.strip() if jd_text and jd_text.strip() else None

    if resume_name is None:
        raise AppError("请上传简历文件", code="INVALID_FILE", http_status=422)
    if jd_data is not None and jd_text:
        raise AppError(
            "JD 请只提供一种形式：文件或文字，不要同时上传",
            code="INVALID_FILE", http_status=422,
        )

    # 快速校验放在 202 之前：用户立刻知道文件合不合规，不用轮询才知道
    _quick_validate(resume_name, resume_data, "简历")
    if jd_name is not None:
        _quick_validate(jd_name, jd_data, "岗位描述")

    session = sessions_repo.create(db, owner_id=visitor.id, question_count=question_count)
    db.commit()

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


# ---------------------------------------------------------------- 查询类


@router.get("/{session_id}", summary="查询进度与可恢复性（只读）")
def get_interview(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
) -> dict:
    session = _owned_session(db, session_id, visitor)

    if session.status == "PREPARING" and session_id not in _preparing:
        # 进程重启导致准备任务孤儿化：如实标记失败，让用户重新上传（§10）
        sessions_repo.mark_preparation_failed(
            db, session, "准备过程被服务重启中断，请重新上传材料"
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


@router.get("/{session_id}/current-question", summary="获取当前题（不推进）")
def get_current_question(
    session_id: str,
    visitor: AnonymousVisitor = Depends(get_visitor),
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
) -> dict:
    session = _owned_session(db, session_id, visitor)

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
    db: OrmSession = Depends(get_db),
    graph=Depends(get_graph),
    redis_client=Depends(get_redis_client),
) -> dict:
    """提交一次回答。同 request_id 重试原样回放；不同 request_id 同题被拒。"""
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
    db: OrmSession = Depends(get_db),
) -> dict:
    session = _owned_session(db, session_id, visitor)

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
        sessions_repo.touch(
            db,
            session,
            status="CANCELLED",
            current_question_id=None,
            completion_reason="USER_ENDED",
            finished=True,
            progress={"stage": "FINISHED", "ended_by": "user"},
        )
        sessions_repo.bump_state_version(db, session)
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
    db: OrmSession = Depends(get_db),
) -> dict:
    """已完成的问答记录。不包含内部评分要点（rubric），也不做总结报告。"""
    session = _owned_session(db, session_id, visitor)

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
