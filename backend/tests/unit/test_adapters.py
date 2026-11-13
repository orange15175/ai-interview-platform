"""模型适配层测试：mock 行为 + 真实适配器的拒配保护。"""

import pytest

from app.agents.adapters import (
    DeepSeekAdapter,
    MockTextAdapter,
    MockVisionAdapter,
    QwenVisionAdapter,
    _guess_mime,
    get_text_adapter,
    get_vision_adapter,
)


def test_mock_text_adapter_returns_canned_json():
    a = MockTextAdapter()
    out = a.complete("你是评分官 @mock:evaluation", "回答内容", temperature=0.2)
    assert '"score"' in out


def test_mock_vision_adapter_returns_materials():
    out = MockVisionAdapter().complete("解析简历 @mock:materials", "user", images=[b"\xff\xd8\xffabc"])
    assert '"resume"' in out and '"skills"' in out


def test_factory_returns_mock_when_env_set():
    """conftest 设了 LLM_MOCK=1，工厂必须给 mock（防止测试误烧真钱）。"""
    assert isinstance(get_text_adapter(), MockTextAdapter)
    assert isinstance(get_vision_adapter(), MockVisionAdapter)


def test_real_adapters_refuse_empty_key(monkeypatch):
    """空 key 不允许构造真实适配器：宁失败，不冒充联调成功（§3.1）。"""
    from app.core.config import TextModelConfig, VisionModelConfig
    with pytest.raises(RuntimeError, match="TEXT_API_KEY"):
        DeepSeekAdapter(TextModelConfig(model="deepseek-chat", base_url="x", api_key=""))
    with pytest.raises(RuntimeError, match="VISION_API_KEY"):
        QwenVisionAdapter(VisionModelConfig(model="qwen3.8-omni-flash", base_url="x", api_key=""))


def test_guess_mime_by_magic():
    assert _guess_mime(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert _guess_mime(b"\x89PNG\r\n") == "image/png"
    assert _guess_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    assert _guess_mime(b"not-an-image") == "application/octet-stream"
