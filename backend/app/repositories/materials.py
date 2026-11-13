"""解析材料仓储（开发说明 §8 interview_materials）。

只存**解析后的结构化结果**与必要的原文证据片段：
- 不存原始二进制/Base64（原始文件在解析 finally 里已清理，§4.2）；
- 联系人信息（姓名/电话/邮箱/证件号）不进库，解析 Agent 已主动剔除；
- 一场会话一份材料（session_id 唯一），重复保存走覆盖（幂等）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import InterviewMaterial
from app.schemas.materials import ParsedMaterials


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def get(db: OrmSession, session_id: uuid.UUID | str) -> InterviewMaterial | None:
    stmt = select(InterviewMaterial).where(
        InterviewMaterial.session_id == _as_uuid(session_id)
    )
    return db.scalars(stmt).one_or_none()


def save_parsed(
    db: OrmSession,
    *,
    session_id: uuid.UUID | str,
    parsed: ParsedMaterials,
    parse_model: str,
    prompt_version: str = "v1",
    schema_version: str = "1",
) -> InterviewMaterial:
    """写入/覆盖解析结果。同一会话重复解析（用户重新上传）时覆盖旧结果。"""
    sid = _as_uuid(session_id)
    row = get(db, sid)
    if row is None:
        row = InterviewMaterial(
            id=uuid.uuid4(),
            session_id=sid,
            schema_version=schema_version,
            parse_model=parse_model,
            prompt_version=prompt_version,
        )
        db.add(row)

    row.schema_version = schema_version
    row.resume_profile = parsed.resume.model_dump()
    row.jd_profile = parsed.jd.model_dump() if parsed.jd is not None else None
    row.evidence = [e.model_dump() for e in parsed.evidence]
    row.uncertain_fields = [u.model_dump() for u in parsed.uncertain_fields]
    row.warnings = list(parsed.warnings)
    row.parse_model = parse_model
    row.prompt_version = prompt_version
    db.flush()
    return row
