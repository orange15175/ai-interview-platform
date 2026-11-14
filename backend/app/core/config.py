"""统一配置中心：新架构所有组件只从这里读配置。

设计原则（对应开发说明文档 §3.1 / §11）：
- 双模型独立配置：文字任务（出题/评分/追问）走 DeepSeek，解析走 Qwen 视觉模型。
  两套 base_url / api_key 分开，互不复用。
- 密钥只存在于本地 .env，不写代码、不进 git、不进日志。
- 旧原型的 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME 三个键仍然兼容：
  当 TEXT_* 未配置时自动回退到旧键，保证旧流程不至于断掉。
- LLM_MOCK=1 时全局使用 mock 适配器，无真实密钥也能跑通全部流程与测试。
"""

import os
import secrets
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# backend/.env 的绝对路径（跟启动目录无关）
ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH, override=False)  # 已存在的进程环境变量优先，不覆盖


def _env(key: str, default: str = "") -> str:
    """读环境变量并去掉首尾空白；空串表示未配置。"""
    return os.getenv(key, default).strip()


# ---------------------------------------------------------------------------
# 登录令牌签名密钥的兜底值
# ---------------------------------------------------------------------------

# 进程内生成一次：保证同一进程内多次 get_settings() 得到同一个密钥，
# 否则每次调用都换密钥，登录令牌立刻失效。进程重启后变化是预期行为
# （.env 里配了 AUTH_SECRET 就不会有这个问题）。
_EPHEMERAL_AUTH_SECRET = secrets.token_hex(32)
_warned_ephemeral = False


def _ephemeral_auth_secret() -> str:
    global _warned_ephemeral
    if not _warned_ephemeral:
        _warned_ephemeral = True
        warnings.warn(
            "未配置 AUTH_SECRET，已使用进程内随机密钥：服务重启后所有登录态失效。"
            "生产环境必须在 .env 里设置 AUTH_SECRET。",
            RuntimeWarning,
            stacklevel=2,
        )
    return _EPHEMERAL_AUTH_SECRET


# ---------------------------------------------------------------------------
# 文字模型（出题 / 评分 / 追问，deepseek-chat）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TextModelConfig:
    model: str
    base_url: str
    api_key: str

    @property
    def is_configured(self) -> bool:
        """有 key 才算配置完成；base_url 缺省时用 DeepSeek 官方地址。"""
        return bool(self.api_key)


def _text_model_config() -> TextModelConfig:
    # 兼容旧键：TEXT_* 未配置时回退到原型用的 OPENAI_* 键
    api_key = _env("TEXT_API_KEY") or _env("OPENAI_API_KEY")
    base_url = _env("TEXT_BASE_URL") or _env("OPENAI_BASE_URL") or "https://api.deepseek.com/v1"
    model = _env("TEXT_MODEL") or _env("MODEL_NAME") or "deepseek-chat"
    return TextModelConfig(model=model, base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------------------
# 视觉/解析模型（qwen3.8-omni-flash，阿里云 MaaS OpenAI 兼容协议）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VisionModelConfig:
    model: str
    base_url: str
    api_key: str

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


def _vision_model_config() -> VisionModelConfig:
    # 用户提供的确切参数：仅支持文本输出，调用时须带 modalities=["text"]
    base_url = _env("VISION_BASE_URL") or "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"
    return VisionModelConfig(
        model=_env("VISION_MODEL") or "qwen3.8-omni-flash",
        base_url=base_url,
        api_key=_env("VISION_API_KEY"),
    )


# ---------------------------------------------------------------------------
# 基础设施（PostgreSQL / Redis）
# ---------------------------------------------------------------------------

# compose.yaml 用 5433/6380 映射，避开本机 5432/6379 上已有服务
DEFAULT_DATABASE_URL = "postgresql+psycopg://interview:interview_dev_pw@localhost:5433/interview"
DEFAULT_REDIS_URL = "redis://localhost:6380/0"


# ---------------------------------------------------------------------------
# 业务默认值（开发说明 §1.3「建议默认」，验收前可调）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessDefaults:
    question_count: int = 15          # 固定 15 道主问题
    max_followups: int = 2            # 每道主问题最多追问 2 次
    max_file_mb: int = 10             # 单文件最大 10 MiB
    max_pdf_pages: int = 10           # PDF 最大 10 页
    max_image_pixels: int = 20_000_000  # 图片最大 2000 万像素
    answer_max_chars: int = 10000     # 单条回答最大长度
    session_ttl_hours: int = 72       # 匿名会话默认有效期（待用户确认留存策略）
    # 阶段 4 并发可靠性：锁租约必须短于模型调用的最坏耗时，靠自动续期兜住（§9.2）
    session_lock_ttl_ms: int = 30_000       # 会话锁租约（30 秒）
    session_lock_wait_ms: int = 800         # 抢锁最长等待（短等待，快速返回 SESSION_BUSY）
    idempotency_ttl_hours: int = 6          # Redis 幂等缓存保留时长（PG 才是正式依据）


def _business_defaults() -> BusinessDefaults:
    return BusinessDefaults(
        question_count=int(_env("QUESTION_COUNT", "15") or 15),
        max_followups=int(_env("MAX_FOLLOWUPS_PER_QUESTION", "2") or 2),
        max_file_mb=int(_env("MAX_FILE_MB", "10") or 10),
        max_pdf_pages=int(_env("MAX_PDF_PAGES", "10") or 10),
        max_image_pixels=int(_env("MAX_IMAGE_PIXELS", "20000000") or 20_000_000),
        answer_max_chars=int(_env("ANSWER_MAX_CHARS", "10000") or 10000),
        session_ttl_hours=int(_env("SESSION_TTL_HOURS", "72") or 72),
        session_lock_ttl_ms=int(_env("SESSION_LOCK_TTL_MS", "30000") or 30000),
        session_lock_wait_ms=int(_env("SESSION_LOCK_WAIT_MS", "800") or 800),
        idempotency_ttl_hours=int(_env("IDEMPOTENCY_TTL_HOURS", "6") or 6),
    )


@dataclass(frozen=True)
class Settings:
    """全局配置快照。启动时构建一次，运行期只读。"""

    text_model: TextModelConfig = field(default_factory=_text_model_config)
    vision_model: VisionModelConfig = field(default_factory=_vision_model_config)
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL") or DEFAULT_DATABASE_URL)
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL") or DEFAULT_REDIS_URL)
    # 环境标识：所有 Redis key 都带它，避免同实例上 dev/test/prod 互相污染（§9.1）
    env_name: str = field(default_factory=lambda: _env("APP_ENV") or "dev")
    # 匿名访客 Cookie 名；生产环境必须 Secure + SameSite=Lax（§11）
    visitor_cookie_name: str = field(default_factory=lambda: _env("VISITOR_COOKIE_NAME") or "aiiv_visitor")
    # 登录态 Cookie 名与有效期（默认 7 天）
    auth_cookie_name: str = field(default_factory=lambda: _env("AUTH_COOKIE_NAME") or "aiiv_user")
    auth_ttl_hours: int = field(default_factory=lambda: int(_env("AUTH_TTL_HOURS", "168") or 168))
    # 登录令牌的签名密钥：只放服务端进程与 .env，绝不进前端/日志。
    # 未配置时用进程内随机值兜底（开发可用，但重启后所有人掉线）。
    auth_secret: str = field(default_factory=lambda: _env("AUTH_SECRET") or _ephemeral_auth_secret())
    cookie_secure: bool = field(default_factory=lambda: _env("COOKIE_SECURE", "0") == "1")
    # 允许跨源写请求的前端地址（本地开发前端 5173 → 后端 8000）；生产同源代理可留空
    allowed_origins: str = field(default_factory=lambda: _env("ALLOWED_ORIGINS"))
    # LLM_MOCK=1 时全部模型调用走 mock 适配器（开发/测试/CI 用）
    llm_mock: bool = field(default_factory=lambda: _env("LLM_MOCK", "0") == "1")
    business: BusinessDefaults = field(default_factory=_business_defaults)

    @property
    def llm_ready(self) -> bool:
        """mock 模式或双模型都配好 key 时，模型层视为就绪。"""
        if self.llm_mock:
            return True
        return self.text_model.is_configured and self.vision_model.is_configured

    def describe(self) -> dict:
        """给 /api/health 的脱敏描述：只说配没配、用哪个模型，绝不带 key。"""
        return {
            "mock": self.llm_mock,
            "text_model": {
                "model": self.text_model.model,
                "configured": self.text_model.is_configured,
            },
            "vision_model": {
                "model": self.vision_model.model,
                "configured": self.vision_model.is_configured,
            },
        }


def get_settings() -> Settings:
    """每次调用重新读取环境（便于测试 monkeypatch 与热更新；运行期开销可忽略）。"""
    return Settings()
