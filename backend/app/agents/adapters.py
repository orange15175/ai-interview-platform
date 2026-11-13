"""模型适配层：文字任务（DeepSeek）+ 解析任务（Qwen Omni）+ Mock。

设计（开发说明 §3.1 / §2「提供商适配器」）：
- 两个真实适配器都走 OpenAI 兼容协议（/v1/chat/completions）。
  差异在于：视觉模型按用户提供的官方样例，必须带 modalities=["text"]，
  且用流式调用累积文本（服务端对 omni 系模型只保证流式文本输出）。
- Mock 适配器不联网、不花钱、结果确定，供阶段 2~4 的全流程测试使用；
  真实联调时 LLM_MOCK=0。绝不拿 mock 冒充真实识别效果（§16）。
- 所有适配器只返回字符串，JSON 解析统一交给 agents 里的 Pydantic 校验
  （复用 app/llm_utils.py 的严格解析），失败抛 ModelOutputError。
"""

from __future__ import annotations

import base64
import json
from typing import Protocol

from openai import OpenAI

from app.core.config import TextModelConfig, VisionModelConfig, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class TextModelAdapter(Protocol):
    """文字模型接口：出题、评分、追问共用。"""

    def complete(self, system: str, user: str, *, temperature: float,
                 max_tokens: int | None = None, json_mode: bool = False) -> str: ...


class VisionModelAdapter(Protocol):
    """解析模型接口：输入文字 + 图片字节，输出文本（通常是 JSON）。"""

    def complete(self, system: str, user: str, *, images: list[bytes],
                 temperature: float = 0.1, json_mode: bool = False) -> str: ...


# ---------------------------------------------------------------------------
# DeepSeek（文字：出题 / 评分 / 追问）
# ---------------------------------------------------------------------------


class DeepSeekAdapter:
    def __init__(self, cfg: TextModelConfig):
        if not cfg.api_key:
            raise RuntimeError("TEXT_API_KEY 未配置：拒绝用空 key 冒充已联调（改用 LLM_MOCK=1）")
        self._model = cfg.model
        self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=120.0, max_retries=1)

    def complete(self, system: str, user: str, *, temperature: float,
                 max_tokens: int | None = None, json_mode: bool = False) -> str:
        kwargs: dict = {}
        if json_mode:
            # DeepSeek 原生支持 json_object 输出模式
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
            **kwargs,
        )
        return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Qwen Omni-Flash（视觉/多模态：简历与 JD 解析）
# ---------------------------------------------------------------------------

_MIME_BY_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"BM", "image/bmp"),
    (b"RIFF", "image/webm"),  # webp 头是 RIFF....WEBP，下面再精判
]


def _guess_mime(data: bytes) -> str:
    """按文件魔数猜图片 MIME（§4.1：不信任客户端 Content-Type）。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    return "application/octet-stream"


class QwenVisionAdapter:
    def __init__(self, cfg: VisionModelConfig):
        if not cfg.api_key:
            raise RuntimeError("VISION_API_KEY 未配置：拒绝用空 key 冒充已联调（改用 LLM_MOCK=1）")
        self._model = cfg.model
        self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=180.0, max_retries=1)

    def complete(self, system: str, user: str, *, images: list[bytes],
                 temperature: float = 0.1, json_mode: bool = False) -> str:
        # 按官方样例：modalities=["text"] + 流式，逐块拼接
        content: list[dict] = [{"type": "text", "text": user}]
        for img in images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{_guess_mime(img)};base64,{b64}"},
            })
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content if images else user},
            ],
            modalities=["text"],          # 仅文本输出（官方说明 omni-flash 不支持 audio）
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks: list[str] = []
        usage = None
        for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    chunks.append(delta)
        if usage is not None:
            # 用量只进日志（脱敏要求 §11），不落业务表
            log.info("视觉模型 token 用量: prompt=%s total=%s", usage.prompt_tokens, usage.total_tokens)
        return "".join(chunks)


# ---------------------------------------------------------------------------
# Mock（无真实 key 的全流程测试）
# ---------------------------------------------------------------------------


class MockTextAdapter:
    """确定性 mock：按调用方在 system prompt 里声明的场景标记返回 canned JSON。"""

    def complete(self, system: str, user: str, *, temperature: float,
                 max_tokens: int | None = None, json_mode: bool = False) -> str:
        if "@mock:questions" in system:
            # 从提示词里的「题目数量：N」解析出题数，让 15 题校验也能被测到
            import re
            m = re.search(r"题目数量[：:]\s*(\d+)", user)
            n = int(m.group(1)) if m else 1
            return _mock_questions_json(n)
        if "@mock:evaluation" in system:
            return _MOCK_EVALUATION_JSON
        if "@mock:followup" in system:
            # 从提示词里的「当前问题」派生追问，保证不同主问题的追问互不重复。
            # 引用片段要去掉问号：FollowUpQuestion 强制单问号，引用原题干会误伤
            import re
            m = re.search(r'"当前问题"\s*:\s*"([^"]*)"', user)
            q = (m.group(1) if m else "该系统")[:26].rstrip("？！?!.。；;，, ")
            return json.dumps(
                {"content": f"你刚才提到「{q}」，当时是怎么测量 QPS 和 P99 延迟的？"},
                ensure_ascii=False,
            )
        return "mock-ok"


def _mock_questions_json(n: int) -> str:
    """生成 n 道结构合法、编号连续、内容互不重复的 mock 题目。"""
    import json
    questions = [
        {
            "main_index": i,
            "category": ["项目经验", "专业能力", "场景解决"][i % 3],
            "difficulty": ["easy", "medium", "hard"][i % 3],
            "content": f"第 {i} 题：请结合 mock 项目说明第 {i} 个技术决策的权衡过程。",
            "material_evidence_ids": ["ev-1"],
            "evaluation_rubric": [
                {"point": "讲清背景与权衡", "weight": 0.5},
                {"point": "有量化结果", "weight": 0.5},
            ],
        }
        for i in range(1, n + 1)
    ]
    return json.dumps({"questions": questions}, ensure_ascii=False)


class MockVisionAdapter:
    def complete(self, system: str, user: str, *, images: list[bytes],
                 temperature: float = 0.1, json_mode: bool = False) -> str:
        return _MOCK_MATERIALS_JSON


# 阶段 2/3 各节点的 mock 数据（结构即文档 §4.2 / §5 / §7.1 / §7.3 的输出 schema）
_MOCK_MATERIALS_JSON = """
{
  "resume": {
    "education": [{"school": "测试大学", "degree": "本科", "major": "计算机"}],
    "experiences": [{"company": "测试公司", "role": "后端开发", "duration": "2023-2025", "highlights": ["mock"]}],
    "projects": [{"name": "mock 项目", "stack": ["Python", "FastAPI"], "highlights": ["mock"]}],
    "skills": ["Python", "FastAPI", "LangGraph", "PostgreSQL"]
  },
  "jd": null,
  "evidence": [{"id": "ev-1", "source": "resume", "page": null, "quote": "mock 依据"}],
  "uncertain_fields": [],
  "warnings": []
}
"""

_MOCK_QUESTIONS_JSON = """
{
  "questions": [
    {"main_index": %d, "category": "项目经验", "difficulty": "medium",
     "content": "请介绍 mock 项目中最有挑战的技术决策。",
     "material_evidence_ids": ["ev-1"],
     "evaluation_rubric": [{"point": "讲清背景与权衡", "weight": 0.5}, {"point": "有量化结果", "weight": 0.5}]}
  ]
}
""" % 1
# 注：mock 只回 1 道题（数量校验节点会按 QUESTION_COUNT 校验，测试里配小题数即可）

_MOCK_EVALUATION_JSON = """
{
  "score": 80,
  "feedback": "回答覆盖了主要要点，缺少量化细节。",
  "rubric_results": [{"point": "讲清背景与权衡", "met": "partial", "note": "有背景无权衡"}],
  "dimension_scores": {"correctness": 80, "completeness": 70, "logic": 80, "depth": 75, "clarity": 85},
  "missing_points": ["缺少量化结果"],
  "follow_up_suggested": true,
  "follow_up_reason": "关键指标未量化",
  "follow_up_target": "项目性能指标"
}
"""


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def get_text_adapter():
    """出题/评分/追问用。LLM_MOCK=1 时全局 mock。"""
    s = get_settings()
    if s.llm_mock:
        return MockTextAdapter()
    return DeepSeekAdapter(s.text_model)


def get_vision_adapter():
    """解析用。LLM_MOCK=1 时全局 mock。"""
    s = get_settings()
    if s.llm_mock:
        return MockVisionAdapter()
    return QwenVisionAdapter(s.vision_model)
