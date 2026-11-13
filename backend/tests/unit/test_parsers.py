"""解析层测试：真实生成 DOCX/PDF/图片后在内存里跑一遍（不落盘、不联网）。"""

import io

import pytest
from PIL import Image

from app.core.config import BusinessDefaults
from app.parsers.document import extract_docx, extract_pdf, normalize_image
from app.parsers.files import FileValidationError, validate_upload

LIMITS = BusinessDefaults()


def _make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("张三的简历", level=1)
    d.add_paragraph("技能：Python、FastAPI、PostgreSQL")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "公司"
    table.cell(0, 1).text = "职位"
    table.cell(1, 0).text = "测试公司"
    table.cell(1, 1).text = "后端开发"
    d.add_paragraph("项目：基于 LangGraph 的多 agent 客服系统")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _make_text_pdf() -> bytes:
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Senior backend engineer with Python and FastAPI experience.")
    buf = doc.tobytes()
    doc.close()
    return buf


def _make_mixed_pdf() -> bytes:
    """第 1 页有文字，第 2 页是图片（模拟扫描页）。"""
    import pymupdf as fitz

    png = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(png, format="PNG")
    png_bytes = png.getvalue()

    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "This page has selectable text content for extraction test.")
    p2 = doc.new_page()
    p2.insert_image(fitz.Rect(40, 40, 560, 400), stream=png_bytes)
    buf = doc.tobytes()
    doc.close()
    return buf


def _make_png(size=(300, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


def test_validate_docx_ok():
    info = validate_upload("简历.docx", _make_docx(), LIMITS)
    assert info.kind == "docx"


def test_validate_pdf_ok_and_page_limit():
    data = _make_text_pdf()
    info = validate_upload("简历.pdf", data, LIMITS)
    assert info.kind == "pdf" and info.page_count == 1

    tight = BusinessDefaults(max_pdf_pages=0)
    with pytest.raises(FileValidationError) as exc:
        validate_upload("简历.pdf", data, tight)
    assert exc.value.code == "INVALID_FILE"


def test_disguised_pdf_rejected():
    """扩展名是 PDF 但内容是纯文本 —— 必须拦下，不能把垃圾送模型。"""
    with pytest.raises(FileValidationError) as exc:
        validate_upload("假的.pdf", b"this is not a pdf at all", LIMITS)
    assert "不是 PDF" in exc.value.message


def test_doc_extension_rejected_with_hint():
    with pytest.raises(FileValidationError) as exc:
        validate_upload("旧简历.doc", b"\xd0\xcf\x11\xe0", LIMITS)
    assert ".docx" in exc.value.message


def test_oversized_file_rejected():
    small_limit = BusinessDefaults(max_file_mb=0)
    with pytest.raises(FileValidationError) as exc:
        validate_upload("简历.pdf", _make_text_pdf(), small_limit)
    assert exc.value.code == "FILE_TOO_LARGE"


def test_too_small_image_rejected():
    with pytest.raises(FileValidationError, match="过小"):
        validate_upload("照片.png", _make_png((100, 100)), LIMITS)


def test_docx_extract_preserves_order_and_tables():
    texts, images, warnings = extract_docx(_make_docx(), "resume", "简历.docx")
    assert len(texts) == 1 and not images
    body = texts[0].text
    # 段落与表格按文档顺序出现
    assert body.index("技能") < body.index("测试公司") < body.index("LangGraph")
    assert "后端开发" in body


def test_pdf_mixed_pages_handled_per_page():
    """混合 PDF：第 1 页提文字，第 2 页渲染成图片（不允许只看第一页）。"""
    texts, renders, warnings = extract_pdf(_make_mixed_pdf(), "resume", "简历.pdf", LIMITS)
    assert len(texts) == 1 and texts[0].page == 1
    assert len(renders) == 1 and renders[0].page == 2
    assert renders[0].data.startswith(b"\x89PNG")
    assert any("转为图片" in w for w in warnings)


def test_normalize_image_downscales_large_photo():
    big = _make_png((4000, 3000))
    renders, warnings = normalize_image(big, "resume", "照片.png")
    assert len(renders) == 1
    with Image.open(io.BytesIO(renders[0].data)) as img:
        assert max(img.size) <= 2000
    assert any("缩放" in w for w in warnings)


def test_normalize_image_keeps_small_photo():
    renders, warnings = normalize_image(_make_png((800, 600)), "resume", "照片.png")
    with Image.open(io.BytesIO(renders[0].data)) as img:
        assert img.size == (800, 600)
    assert warnings == []
