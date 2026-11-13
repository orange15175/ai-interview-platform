"""上传校验（开发说明 §4.1）。

原则：
- 不信任客户端 Content-Type 与文件扩展名：三者（扩展名 / 魔数 / 能否真正打开）交叉验证。
- 先看大小再看内容；不落盘，全程在内存里处理（原始文件解析后即丢弃）。
- 错误必须是"可操作"的：告诉用户哪个文件、什么原因、怎么改。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from PIL import Image

from app.core.config import BusinessDefaults
from app.core.errors import AppError


class FileValidationError(AppError):
    """文件不合规。message 会直接展示给用户，必须是人话。"""

    code = "INVALID_FILE"
    http_status = 422

    def __init__(self, code: str, message: str):
        super().__init__(message, code=code)


@dataclass
class UploadInfo:
    filename: str          # 仅用于提示与日志，绝不拼路径
    kind: str              # docx / pdf / image
    size: int
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)


EXT_KIND = {
    ".docx": "docx",
    ".pdf": "pdf",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
}
# 旧版 .doc 明确不支持（开发说明 §1.3 建议默认），给出可操作提示
DOC_EXT = {".doc"}


def _ext(filename: str) -> str:
    name = filename.lower()
    idx = name.rfind(".")
    return name[idx:] if idx >= 0 else ""


def validate_upload(filename: str, data: bytes, limits: BusinessDefaults) -> UploadInfo:
    """校验并返回文件画像。任何不合规都抛 FileValidationError（带稳定错误码）。"""
    ext = _ext(filename or "")
    if not filename:
        raise FileValidationError("INVALID_FILE", "未提供文件名")

    # 1) 大小（10 MiB 默认，可配置）
    max_bytes = limits.max_file_mb * 1024 * 1024
    if len(data) == 0:
        raise FileValidationError("INVALID_FILE", f"{filename} 是空文件")
    if len(data) > max_bytes:
        raise FileValidationError(
            "FILE_TOO_LARGE",
            f"{filename} 超过 {limits.max_file_mb} MiB 上限（当前 {len(data) / 1024 / 1024:.1f} MiB）",
        )

    if ext in DOC_EXT:
        raise FileValidationError(
            "INVALID_FILE",
            f"{filename} 是旧版 .doc 格式，请另存为 .docx 后再上传",
        )

    kind = EXT_KIND.get(ext)
    if kind is None:
        raise FileValidationError(
            "INVALID_FILE",
            f"{filename} 格式不支持，简历请用 DOCX / PDF / 图片（JPG/PNG/WEBP）",
        )

    # 2) 魔数与扩展名必须一致（防伪装文件）
    if kind == "pdf" and not data.startswith(b"%PDF"):
        raise FileValidationError("INVALID_FILE", f"{filename} 扩展名是 PDF，但内容不是 PDF")
    if kind == "docx" and not data.startswith(b"PK\x03\x04"):
        raise FileValidationError("INVALID_FILE", f"{filename} 扩展名是 DOCX，但内容不是 DOCX")
    if kind == "image" and not _looks_like_image(data):
        raise FileValidationError("INVALID_FILE", f"{filename} 扩展名是图片，但内容不是有效图片")

    # 3) 真正能打开 + 业务限制
    if kind == "pdf":
        return _validate_pdf(filename, data, limits)
    if kind == "docx":
        return _validate_docx(filename, data)
    return _validate_image(filename, data, limits)


def _looks_like_image(data: bytes) -> bool:
    head = data[:12]
    return (
        head.startswith(b"\xff\xd8\xff")          # jpeg
        or head.startswith(b"\x89PNG")            # png
        or head.startswith(b"GIF8")
        or head.startswith(b"BM")
        or head.startswith(b"II*\x00")
        or head.startswith(b"MM\x00*")
        or (head.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _validate_pdf(filename: str, data: bytes, limits: BusinessDefaults) -> UploadInfo:
    import pymupdf  # PyMuPDF（1.26+ 新包名，fitz 已弃用）

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError("INVALID_FILE", f"{filename} 无法打开，可能已损坏") from exc

    if doc.needs_pass:
        doc.close()
        raise FileValidationError("INVALID_FILE", f"{filename} 有密码保护，请先解除密码")
    pages = doc.page_count
    doc.close()

    if pages == 0:
        raise FileValidationError("INVALID_FILE", f"{filename} 没有任何页面")
    if pages > limits.max_pdf_pages:
        raise FileValidationError(
            "INVALID_FILE",
            f"{filename} 共 {pages} 页，超过 {limits.max_pdf_pages} 页限制，请上传精简版",
        )
    return UploadInfo(filename=filename, kind="pdf", size=len(data), page_count=pages)


def _validate_docx(filename: str, data: bytes) -> UploadInfo:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in set(zf.namelist()):
                raise FileValidationError("INVALID_FILE", f"{filename} 不是有效的 Word 文档")
    except zipfile.BadZipFile as exc:
        raise FileValidationError("INVALID_FILE", f"{filename} 已损坏，无法解压") from exc
    return UploadInfo(filename=filename, kind="docx", size=len(data), page_count=None)


def _validate_image(filename: str, data: bytes, limits: BusinessDefaults) -> UploadInfo:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()  # 只校验完整性，verify 后对象不可再用
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            pixels = w * h
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError("INVALID_FILE", f"{filename} 不是有效图片或已损坏") from exc

    if pixels > limits.max_image_pixels:
        raise FileValidationError(
            "INVALID_FILE",
            f"{filename} 尺寸 {w}×{h} 超过 {limits.max_image_pixels // 10000} 万像素上限",
        )
    if min(w, h) < 200:
        raise FileValidationError(
            "INVALID_FILE",
            f"{filename} 尺寸 {w}×{h} 过小，文字会不可辨识，请上传更清晰的原图",
        )
    return UploadInfo(filename=filename, kind="image", size=len(data), page_count=1)
