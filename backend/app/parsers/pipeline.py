"""解析流水线：文件字节 → 统一 ParsedInput（文字块 + 图片块 + 警告）。

只做"把文件变成可送模型的内容"，不做语义理解——理解是 agents/parse_agent 的事。
"""

from __future__ import annotations

from app.core.config import BusinessDefaults, get_settings
from app.parsers.document import extract_docx, extract_pdf, normalize_image
from app.parsers.files import FileValidationError, validate_upload
from app.parsers.types import ParsedInput, RenderBlock, TextBlock

# JD 文本最短有效长度：低于这个值视为无效输入（避免只有一个"无"字）
JD_TEXT_MIN_CHARS = 10

# 送模型的文字总量上限：一份 10 MiB 的 docx 能塞进几十万字，
# 不截断就会超时并烧钱。截断必须留痕（warnings），不能假装全读了。
MAX_TEXT_CHARS = 60_000


def validate_jd_text(jd_text: str | None) -> str | None:
    """JD 文字的入参校验，**必须在返回 202 之前调用**。

    以前这段校验藏在后台准备任务里：接口先回 202，用户等半分钟才看到
    "JD 文本太短"，而且整场面试就此失败。校验本身是纯字符串判断，
    没有任何理由放到后台。
    """
    if jd_text is None:
        return None
    clean = jd_text.strip()
    if not clean:
        return None  # 只填空格等于没填，按"未提供 JD"处理
    if len(clean) < JD_TEXT_MIN_CHARS:
        raise FileValidationError(
            "INVALID_JD_TEXT",
            f"岗位描述文字太短（至少 {JD_TEXT_MIN_CHARS} 个字）。"
            "请补全岗位职责与要求，或改为上传 JD 文件",
        )
    return clean


def _extract(filename: str, data: bytes, source: str, limits: BusinessDefaults) -> ParsedInput:
    info = validate_upload(filename, data, limits)
    label = filename
    if info.kind == "docx":
        texts, images, warns = extract_docx(data, source, label, limits)
    elif info.kind == "pdf":
        texts, images, warns = extract_pdf(data, source, label, limits)
    else:
        images, warns = normalize_image(data, source, label)
        texts = []
    return ParsedInput(text_blocks=texts, render_blocks=images, warnings=warns)


def build_parsed_input(
    *,
    resume_filename: str,
    resume_bytes: bytes,
    jd_filename: str | None = None,
    jd_bytes: bytes | None = None,
    jd_text: str | None = None,
    limits: BusinessDefaults | None = None,
) -> ParsedInput:
    """组装解析输入。简历必填；JD 支持图片文件或纯文字，二者互斥。"""
    limits = limits or get_settings().business

    if jd_bytes is not None and jd_text and jd_text.strip():
        raise FileValidationError("INVALID_FILE", "JD 请只提供一种形式：图片文件或文字，不要同时上传")

    if not resume_bytes:
        raise FileValidationError("INVALID_FILE", "简历是必填项，请先上传简历")

    resume = _extract(resume_filename, resume_bytes, "resume", limits)
    if not resume.has_content:
        raise FileValidationError(
            "UNREADABLE_MATERIAL",
            "简历没有可用内容（可能是空白文档或无法识别的扫描件），请换一份文件试试",
        )

    merged = ParsedInput(
        text_blocks=list(resume.text_blocks),
        render_blocks=list(resume.render_blocks),
        warnings=list(resume.warnings),
    )

    # JD：文件优先于文字（调用方保证互斥）
    if jd_bytes is not None and jd_filename:
        jd = _extract(jd_filename, jd_bytes, "jd", limits)
        merged.text_blocks.extend(jd.text_blocks)
        merged.render_blocks.extend(jd.render_blocks)
        merged.warnings.extend(jd.warnings)
    elif jd_text and jd_text.strip():
        clean = validate_jd_text(jd_text)
        if clean:
            merged.text_blocks.append(TextBlock(source="jd", label="JD 文本", text=clean))
    else:
        merged.warnings.append("未提供 JD：题目将只依据简历生成，不会虚构岗位要求")

    _cap_text(merged)
    return merged


def _cap_text(parsed: ParsedInput, limit: int = MAX_TEXT_CHARS) -> None:
    """把文字总量压到上限内（按块截断，保留来源标签）。"""
    total = parsed.char_count()
    if total <= limit:
        return
    kept: list[TextBlock] = []
    used = 0
    for block in parsed.text_blocks:
        if used >= limit:
            break
        room = limit - used
        if len(block.text) > room:
            kept.append(
                TextBlock(
                    source=block.source,
                    label=block.label,
                    text=block.text[:room],
                    page=block.page,
                )
            )
            used += room
            break
        kept.append(block)
        used += len(block.text)
    parsed.text_blocks = kept
    parsed.warnings.append(
        f"材料文字共 {total} 字，超过 {limit} 字上限，超出部分未送入模型"
    )


def describe_input(parsed: ParsedInput) -> dict:
    """给日志/状态接口的输入画像（不含内容本身）。"""
    pages = {b.page for b in parsed.text_blocks if b.page} | {
        b.page for b in parsed.render_blocks if b.page
    }
    return {
        "text_blocks": len(parsed.text_blocks),
        "render_blocks": len(parsed.render_blocks),
        "char_count": parsed.char_count(),
        "pages": len(pages),
        "warnings": len(parsed.warnings),
    }
