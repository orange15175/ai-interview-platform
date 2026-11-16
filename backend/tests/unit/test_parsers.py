"""解析层测试：真实生成 DOCX/PDF/图片后在内存里跑一遍（不落盘、不联网）。"""

import io
import zipfile

import pytest
from PIL import Image

from app.core.config import BusinessDefaults
from app.parsers import document, pipeline
from app.parsers.document import extract_docx, extract_pdf, normalize_image
from app.parsers.files import FileValidationError, validate_upload
from app.parsers.types import ParsedInput, TextBlock

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


def test_docx_textboxes_and_compatibility_branches():
    """现代/旧式文本框均可读；备用结构不重复，真实重复文字不丢失。"""
    import docx
    from lxml import etree

    d = docx.Document()
    d.add_paragraph("正文前")
    xml = '''<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
        xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
        xmlns:v="urn:schemas-microsoft-com:vml">
      <w:r><mc:AlternateContent>
        <mc:Choice Requires="wps"><wps:wsp><wps:txbx><w:txbxContent>
          <w:p><w:r><w:t>项目经历</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>项目A</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>Redis</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:txbxContent></wps:txbx></wps:wsp></mc:Choice>
        <mc:Fallback><v:shape><v:textbox><w:txbxContent>
          <w:p><w:r><w:t>项目经历</w:t></w:r></w:p>
        </w:txbxContent></v:textbox></v:shape></mc:Fallback>
      </mc:AlternateContent></w:r>
      <w:r><w:pict><v:shape><v:textbox><w:txbxContent>
        <w:p><w:r><w:t>教育经历</w:t></w:r></w:p>
      </w:txbxContent></v:textbox></v:shape></w:pict></w:r>
    </w:p>'''
    d.element.body.insert(1, etree.fromstring(xml.encode()))
    d.add_paragraph("正文后")
    d.add_paragraph("Redis")
    buf = io.BytesIO()
    d.save(buf)
    texts, _, _ = extract_docx(buf.getvalue(), "resume", "textboxes.docx")
    text = texts[0].text
    assert text.count("项目经历") == 1
    assert text.count("Redis") == 2
    assert "项目A | Redis" in text
    assert text.index("正文前") < text.index("项目经历") < text.index("教育经历") < text.index("正文后")


def test_docx_textbox_in_table_and_alternate_fallback():
    import docx
    from lxml import etree

    d = docx.Document()
    cell = d.add_table(rows=1, cols=1).cell(0, 0)
    cell._tc.append(etree.fromstring(b'''<mc:AlternateContent
        xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
        xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <mc:Choice Requires="unknown"/>
      <mc:Fallback><w:txbxContent><w:p><w:r><w:t>fallback project</w:t></w:r></w:p>
      </w:txbxContent></mc:Fallback></mc:AlternateContent>'''))
    buf = io.BytesIO()
    d.save(buf)
    texts, _, _ = extract_docx(buf.getvalue(), "resume", "table.docx")
    assert "fallback project" in texts[0].text


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


# ---------------------------------------------------------------- DOCX 内嵌图片


def _make_docx_with_image() -> bytes:
    """文字 + 一张内嵌图片：设计稿式简历的典型形态。"""
    import docx

    png = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(png, format="PNG")
    png.seek(0)

    d = docx.Document()
    d.add_paragraph("个人信息：后端工程师")
    d.add_picture(png, width=docx.shared.Inches(4))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_embedded_image_becomes_render_block():
    texts, images, warnings = extract_docx(_make_docx_with_image(), "resume", "简历.docx")
    assert len(texts) == 1
    assert len(images) == 1
    # 标签必须说明来源，模型才知道这张图属于同一份简历
    assert "简历.docx" in images[0].label and "内嵌图片 1" in images[0].label
    with Image.open(io.BytesIO(images[0].data)) as img:
        assert img.size == (600, 400)
    assert any("内嵌图片" in w for w in warnings)


def _fake_docx_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 必须有 document.xml，才像一个 docx
        zf.writestr("word/document.xml", "<w:document/>")
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_docx_image_zip_expansion_bomb_is_rejected():
    """高压缩比条目（解压炸弹）不许被读进内存。"""
    bomb = b"\x00" * (6 * 1024 * 1024)  # 解压 6 MiB，压缩后只有几 KB
    data = _fake_docx_zip({"word/media/image1.png": bomb})

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo("word/media/image1.png")
    assert info.file_size / max(info.compress_size, 1) > 200

    blocks, warnings = document._docx_image_blocks(
        data, "resume", "简历.docx", LIMITS
    )
    assert blocks == []
    assert any("压缩比异常" in w for w in warnings)


def test_docx_image_path_traversal_entry_ignored():
    png = _make_png((300, 300))
    data = _fake_docx_zip({
        "word/media/../../evil.png": png,
        "word/media/image1.png": png,
    })
    blocks, warnings = document._docx_image_blocks(data, "resume", "简历.docx", LIMITS)
    assert len(blocks) == 1  # 只认合法条目
    assert any("可疑压缩包条目" in w for w in warnings)


def test_docx_image_count_and_bytes_limits():
    png = _make_png((300, 300))
    many = {f"word/media/image{i}.png": png for i in range(1, 12)}
    blocks, warnings = document._docx_image_blocks(
        _fake_docx_zip(many), "resume", "简历.docx", LIMITS
    )
    assert len(blocks) == document.DOCX_MAX_IMAGES
    assert any("仅解析前" in w for w in warnings)


def test_corrupt_docx_image_warns_instead_of_crashing():
    data = _fake_docx_zip({"word/media/image1.png": b"not an image at all"})
    blocks, warnings = document._docx_image_blocks(data, "resume", "简历.docx", LIMITS)
    assert blocks == []
    assert any("已损坏" in w for w in warnings)


# ---------------------------------------------------------------- PDF 逐页


def test_text_pdf_extracts_text_only():
    texts, renders, warnings = extract_pdf(_make_text_pdf(), "resume", "简历.pdf", LIMITS)
    assert len(texts) == 1 and texts[0].page == 1
    assert renders == []
    assert warnings == []


def test_scanned_pdf_becomes_image_page():
    import pymupdf as fitz

    png = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(png, format="PNG")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(40, 40, 560, 400), stream=png.getvalue())
    data = doc.tobytes()
    doc.close()

    texts, renders, warnings = extract_pdf(data, "resume", "简历.pdf", LIMITS)
    assert texts == []
    assert len(renders) == 1 and renders[0].page == 1
    assert any("转为图片" in w for w in warnings)


def test_image_exif_orientation_is_applied():
    """手机竖拍照片：EXIF orientation=6 必须转正，否则模型读到横图。"""
    img = Image.new("RGB", (400, 200), "white")
    exif = img.getexif()
    exif[274] = 6  # 顺时针旋转 90°
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)

    renders, _ = normalize_image(buf.getvalue(), "resume", "照片.jpg")
    with Image.open(io.BytesIO(renders[0].data)) as out:
        assert out.size == (200, 400)


def test_text_input_is_capped_with_warning():
    """超长文字不能整段送模型（超时与费用失控），截断必须留痕。"""
    parsed = ParsedInput(
        text_blocks=[TextBlock(source="resume", label="简历.txt", text="字" * 50_000)],
        render_blocks=[],
        warnings=[],
    )
    pipeline._cap_text(parsed, limit=1_000)
    assert parsed.char_count() == 1_000
    assert any("未送入模型" in w for w in parsed.warnings)
