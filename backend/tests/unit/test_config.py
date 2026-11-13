"""配置中心单元测试。"""

from app.core.config import Settings, get_settings


def test_mock_flag_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "1")
    assert get_settings().llm_mock is True


def test_text_model_falls_back_to_legacy_keys(monkeypatch):
    """旧原型的 OPENAI_* 键仍然有效，保证旧流程不因改造断掉。"""
    monkeypatch.setenv("TEXT_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-legacy")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    s = get_settings()
    assert s.text_model.api_key == "sk-legacy"
    assert s.text_model.model == "deepseek-chat"
    assert s.text_model.is_configured is True


def test_vision_model_defaults(monkeypatch):
    """未配置时给文档指定的默认值（qwen3.8-omni-flash / 千问兼容端点）。"""
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("VISION_MODEL", raising=False)
    s = get_settings()
    assert s.vision_model.model == "qwen3.8-omni-flash"
    assert "qianwenaiapi.com" in s.vision_model.base_url
    assert s.vision_model.is_configured is False


def test_describe_never_leaks_keys(monkeypatch):
    """/api/health 的脱敏描述不包含任何 key。"""
    monkeypatch.setenv("TEXT_API_KEY", "sk-secret-text-123")
    monkeypatch.setenv("VISION_API_KEY", "sk-secret-vision-456")
    info = get_settings().describe()
    dumped = str(info)
    assert "sk-secret" not in dumped
    assert info["text_model"]["configured"] is True
    assert info["vision_model"]["configured"] is True


def test_business_defaults(monkeypatch):
    monkeypatch.delenv("QUESTION_COUNT", raising=False)
    monkeypatch.delenv("MAX_FOLLOWUPS_PER_QUESTION", raising=False)
    b = get_settings().business
    assert b.question_count == 15
    assert b.max_followups == 2
    assert b.max_file_mb == 10
    assert b.max_pdf_pages == 10


def test_llm_ready_logic():
    """mock 模式下即使一个 key 都没有也算就绪。"""
    s = Settings()
    assert s.llm_mock is True  # conftest 已设 LLM_MOCK=1
    assert s.llm_ready is True
