"""文档提取：DOCX / PDF / 图片（开发说明 §4.1）。

三个容易踩的坑，这里都处理了：
1. DOCX 的段落和表格要按文档真实顺序读，不能先读所有段落再读所有表格。
2. PDF 必须逐页判断：有文字就提文字，没文字（扫描页）才渲染图片。
   混合 PDF（前几页文字、后几页扫描）不允许"只检查第一页"。
3. 手机拍的照片方向可能记录在 EXIF 里，不旋转就会横着送给模型。
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from app.core.config import BusinessDefaults
from app.parsers.types import RenderBlock, TextBlock

# PDF 页面文字少于这个字符数，判定为扫描页/图片页，转为渲染图片
PDF_TEXT_THRESHOLD = 20
# 渲染扫描页的 DPI：150 够模型识别，又不至于图片过大
PDF_RENDER_DPI = 150
# 送给视觉模型前的最长边上限（超出按比例缩小，避免 token 爆炸）
IMAGE_MAX_SIDE = 2000


def extract_docx(data: bytes, source: str, label: str) -> tuple[list[TextBlock], list[RenderBlock], list[str]]:
    """按文档流顺序抽取段落与表格。嵌入图片不解析，但明确提示。"""
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    warnings: list[str] = []
    document = docx.Document(io.BytesIO(data))
    lines: list[str] = []

    # 按 body 子元素的真实顺序遍历（w:p 段落 / w:tbl 表格）
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text.strip()
            if text:
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                # 合并单元格会重复输出同一文本，去掉相邻重复
                deduped: list[str] = []
                for c in cells:
                    if not deduped or deduped[-1] != c:
                        deduped.append(c)
                row_text = " | ".join(c for c in deduped if c)
                if row_text:
                    lines.append(row_text)

    inline_images = len(document.inline_shapes)
    if inline_images:
        warnings.append(f"{label} 含 {inline_images} 张嵌入图片，文字内容已提取，图片内文字未解析")

    text = "\n".join(lines)
    if not text.strip():
        return [], [], warnings + [f"{label} 未提取到任何文字（可能整篇都是图片）"]

    return [TextBlock(source=source, label=label, text=text)], [], warnings


def extract_pdf(data: bytes, source: str, label: str, limits: BusinessDefaults) -> tuple[list[TextBlock], list[RenderBlock], list[str]]:
    """逐页判断：有文字提文字，没文字渲染成图片。"""
    import pymupdf as fitz

    warnings: list[str] = []
    text_blocks: list[TextBlock] = []
    render_blocks: list[RenderBlock] = []

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for i, page in enumerate(doc):
            page_no = i + 1
            text = page.get_text("text").strip()
            if len(text) >= PDF_TEXT_THRESHOLD:
                text_blocks.append(TextBlock(
                    source=source, label=f"{label} 第 {page_no} 页", text=text, page=page_no,
                ))
            else:
                # 扫描页：渲染成 PNG 交视觉模型
                pix = page.get_pixmap(dpi=PDF_RENDER_DPI)
                render_blocks.append(RenderBlock(
                    source=source, label=f"{label} 第 {page_no} 页（扫描件）",
                    data=pix.tobytes("png"), page=page_no,
                ))
                warnings.append(f"{label} 第 {page_no} 页无有效文字，已转为图片解析")
    finally:
        doc.close()

    return text_blocks, render_blocks, warnings


def normalize_image(data: bytes, source: str, label: str) -> tuple[list[RenderBlock], list[str]]:
    """校正方向、限制尺寸，输出可直接送模型的 JPEG/PNG 字节。"""
    warnings: list[str] = []
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)  # 手机照片方向校正
        if img.mode not in ("RGB", "L"):
            # 透明背景转白底，避免 PNG 透明变黑
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        elif img.mode == "L":
            img = img.convert("RGB")

        w, h = img.size
        longest = max(w, h)
        if longest > IMAGE_MAX_SIDE:
            ratio = IMAGE_MAX_SIDE / longest
            new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
            img = img.resize(new_size, Image.LANCZOS)
            warnings.append(f"{label} 尺寸过大，已缩放至 {new_size[0]}×{new_size[1]} 解析")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        normalized = buf.getvalue()

    return [RenderBlock(source=source, label=label, data=normalized)], warnings
