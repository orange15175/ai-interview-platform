"""解析流水线：文件字节 → 统一 ParsedInput（文字块 + 图片块 + 警告）。

只做"把文件变成可送模型的内容"，不做语义理解——理解是 agents/parse_agent 的事。
"""

from __future__ import annotations

from app.core.config import BusinessDefaults, get_settings
from app.parsers.document import extract_docx, extract_pdf, normalize_image
from app.parsers.files import FileValidationError, validate_upload
from app.parsers.types import ParsedInput, RenderBlock, TextBlock

# JD 文本最短有效长度：低于这个值视为没提供（避免只有一个"无"字）
JD_TEXT_MIN_CHARS = 10


def _extract(filename: str, data: bytes, source: str, limits: BusinessDefaults) -> ParsedInput:
    info = validate_upload(filename, data, limits)
    label = filename
    if info.kind == "docx":
        texts, images, warns = extract_docx(data, source, label)
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
        clean = jd_text.strip()
        if len(clean) < JD_TEXT_MIN_CHARS:
            raise FileValidationError("INVALID_FILE", "JD 文本太短，无法作为岗位描述使用")
        merged.text_blocks.append(TextBlock(source="jd", label="JD 文本", text=clean))
    else:
        merged.warnings.append("未提供 JD：题目将只依据简历生成，不会虚构岗位要求")

    return merged


def describe_input(parsed: ParsedInput) -> dict:
    """给日志/状态接口的输入画像（不含内容本身）。"""
    return {
        "text_blocks": len(parsed.text_blocks),
        "render_blocks": len(parsed.render_blocks),
        "char_count": parsed.char_count(),
        "warnings": len(parsed.warnings),
    }
