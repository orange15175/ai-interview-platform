"""解析输入的统一数据结构。

区分两类内容：
- TextBlock：已经从文档里提取出的文字（带来源与页码，供 evidence 定位）。
- RenderBlock：需要交给视觉模型的图片（扫描页渲染结果或用户上传的原图）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextBlock:
    source: str          # resume / jd
    label: str           # 人类可读来源，如「简历.pdf 第 2 页」
    text: str
    page: int | None = None


@dataclass
class RenderBlock:
    source: str
    label: str
    data: bytes          # 图片字节（PNG/JPEG），不落盘
    page: int | None = None


@dataclass
class ParsedInput:
    """解析 Agent 的完整输入：文字块 + 图片块 + 提示信息。"""

    text_blocks: list[TextBlock]
    render_blocks: list[RenderBlock]
    warnings: list[str]

    @property
    def has_content(self) -> bool:
        """判断是否至少存在非空文字或图片，避免空文件继续调用模型。"""
        return bool(self.text_blocks or self.render_blocks)

    def char_count(self) -> int:
        """统计全部文字块字符数，用于输入上限和准备进度展示。"""
        return sum(len(b.text) for b in self.text_blocks)
