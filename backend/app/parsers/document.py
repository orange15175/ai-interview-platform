"""文档提取：DOCX / PDF / 图片（开发说明 §4.1 / §五 / §六）。

四个容易踩的坑，这里都处理了：
1. DOCX 的段落和表格要按文档真实顺序读，不能先读所有段落再读所有表格。
2. DOCX 里贴图当简历很常见（设计稿式简历、扫描后插进 Word 的简历）。以前只
   提文字，这类简历等于"没有内容"。现在从 ZIP 的 word/media/ 取图交给视觉模型。
3. PDF 必须逐页判断：有文字就提文字，没文字（扫描页）才渲染图片。
   混合 PDF（前几页文字、后几页扫描）不允许"只检查第一页"。
4. 手机拍的照片方向可能记录在 EXIF 里，不旋转就会横着送给模型。

ZIP 是攻击面：条目数、单条解压大小、总解压大小、压缩比、路径穿越都要卡死，
否则一个 10 MiB 的 docx 能解压出几个 GB（解压炸弹）把进程打死。
"""

from __future__ import annotations

import io
import zipfile

from PIL import Image, ImageOps

from app.core.config import BusinessDefaults
from app.parsers.types import RenderBlock, TextBlock

# PDF 页面文字少于这个字符数，判定为扫描页/图片页，转为渲染图片
PDF_TEXT_THRESHOLD = 20
# 渲染扫描页的 DPI：150 够模型识别，又不至于图片过大
PDF_RENDER_DPI = 150
# 送给视觉模型前的最长边上限（超出按比例缩小，避免 token 爆炸）
IMAGE_MAX_SIDE = 2000

# ---- DOCX 内嵌图片的硬闸（防解压炸弹 / 恶意条目）----
DOCX_MAX_ZIP_ENTRIES = 512                    # 条目数上限
DOCX_MAX_MEMBER_BYTES = 8 * 1024 * 1024       # 单条解压后上限
DOCX_MAX_TOTAL_BYTES = 40 * 1024 * 1024       # 全部解压后总量上限
DOCX_MAX_COMPRESSION_RATIO = 200              # 单条压缩比上限
DOCX_MAX_IMAGES = 8                           # 最多解析多少张内嵌图片（控 token）
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")


def _docx_image_blocks(
    data: bytes, source: str, label: str, limits: BusinessDefaults
) -> tuple[list[RenderBlock], list[str]]:
    """从 DOCX 的 word/media/ 取图片，校验后转成可送视觉模型的字节。

    安全顺序是有意为之：先看 inflate 后的大小（infolist 里已记录），
    确认没超限才真正 read()。反过来"先读再检查"就等于自己给自己造炸弹。
    """
    blocks: list[RenderBlock] = []
    warnings: list[str] = []
    total = 0

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > DOCX_MAX_ZIP_ENTRIES:
                return [], [f"{label} 内部条目过多（{len(infos)}），已跳过内嵌图片解析"]

            candidates = []
            for info in infos:
                name = info.filename
                # 路径穿越 / 绝对路径一律不碰（正常 docx 不会出现）
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                    warnings.append(f"{label} 含可疑压缩包条目，已忽略")
                    continue
                if not name.startswith("word/media/"):
                    continue
                if name.endswith("/") or not name.lower().endswith(_IMAGE_EXT):
                    continue
                candidates.append(info)

            if not candidates:
                return [], []

            if len(candidates) > DOCX_MAX_IMAGES:
                warnings.append(
                    f"{label} 含 {len(candidates)} 张内嵌图片，仅解析前 {DOCX_MAX_IMAGES} 张"
                )
                candidates = candidates[:DOCX_MAX_IMAGES]

            for index, info in enumerate(candidates, start=1):
                size = info.file_size
                if size <= 0:
                    continue
                if size > DOCX_MAX_MEMBER_BYTES:
                    warnings.append(f"{label} 第 {index} 张内嵌图片过大，已跳过")
                    continue
                total += size
                if total > DOCX_MAX_TOTAL_BYTES:
                    warnings.append(f"{label} 内嵌图片解压总量超限，其余图片已跳过")
                    break
                ratio = size / max(info.compress_size, 1)
                if ratio > DOCX_MAX_COMPRESSION_RATIO:
                    warnings.append(f"{label} 第 {index} 张内嵌图片压缩比异常，已跳过")
                    continue

                try:
                    raw = zf.read(info)
                except (zipfile.BadZipFile, OSError):
                    warnings.append(f"{label} 第 {index} 张内嵌图片无法读取，已跳过")
                    continue

                try:
                    renders, note = normalize_image(
                        raw, source, f"{label} 内嵌图片 {index}"
                    )
                except Exception:  # noqa: BLE001 —— 单张坏了不影响其余内容
                    warnings.append(f"{label} 第 {index} 张内嵌图片已损坏，无法识别")
                    continue
                blocks.extend(renders)
                warnings.extend(note)
    except zipfile.BadZipFile:
        return [], [f"{label} 压缩包已损坏，内嵌图片未解析"]

    if blocks:
        warnings.append(
            f"{label} 含 {len(blocks)} 张内嵌图片，已与文字一起交给解析模型"
        )
    return blocks, warnings


def extract_docx(
    data: bytes, source: str, label: str, limits: BusinessDefaults | None = None
) -> tuple[list[TextBlock], list[RenderBlock], list[str]]:
    """按文档流顺序抽取段落与表格，并把内嵌图片抽成渲染块。"""
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    limits = limits or BusinessDefaults()
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

    # 内嵌图片：文字版简历里贴的设计稿/扫描页，必须交给视觉模型
    images, image_warnings = _docx_image_blocks(data, source, label, limits)
    warnings.extend(image_warnings)

    text = "\n".join(lines)
    if not text.strip():
        if images:
            # 整篇都是图片的 DOCX：不算不可读，让视觉模型来读
            warnings.append(f"{label} 未提取到文字，已按内嵌图片解析")
            return [], images, warnings
        return [], [], warnings + [f"{label} 未提取到任何文字或图片（可能是空白文档）"]

    return [TextBlock(source=source, label=label, text=text)], images, warnings


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
