"""
环境变量配置 — 全部从环境变量获取，无硬编码密钥。
"""

import os


def _require_env(key):
    """读取必需的环境变量，不存在则立即报错。"""
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(f"缺少必需的环境变量: {key}")
    return val


def _env(key, default=""):
    """读取可选的环境变量。"""
    return os.environ.get(key, default)


# ---- OSS 配置 ----
OSS_CONFIG = {
    "access_key": _require_env("OSS_ACCESS_KEY"),
    "secret_key": _require_env("OSS_SECRET_KEY"),
    "bucket_name": _env("OSS_BUCKET_NAME", "gzhlaker-papers"),
    "endpoint_internal": _env("OSS_ENDPOINT_INTERNAL", "oss-cn-hongkong-internal.aliyuncs.com"),
    "endpoint_accelerate": _env("OSS_ENDPOINT_ACCELERATE", "oss-accelerate.aliyuncs.com"),
}

# ---- OpenRouter / LLM 配置 ----
LLM_CONFIG = {
    "api_key": _require_env("OPENROUTER_API_KEY"),
    "base_url": _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "model": _env("OPENROUTER_MODEL", "openrouter/auto"),
    "auto_allowed_models": _env("OPENROUTER_AUTO_ALLOWED_MODELS", ""),
}

# ---- Telegram 配置 ----
TELEGRAM_CONFIG = {
    "bot_token": _env("TELEGRAM_BOT_TOKEN", ""),
    "chat_id": _env("TELEGRAM_CHAT_ID", ""),
}

# ---- 滴答清单配置 ----
DIDA_CONFIG = {
    "client_id": _require_env("DIDA_CLIENT_ID"),
    "client_secret": _require_env("DIDA_CLIENT_SECRET"),
}

# ---- 每日速递配置 ----
NEWS_CONFIG = {
    "trending_n":          int(_env("TRENDING_N", "5")),          # GitHub 板块条数
    "news_max":            int(_env("NEWS_MAX_ITEMS", "5")),      # 新闻板块条数
    "news_retention_days": int(_env("NEWS_RETENTION_DAYS", "7")), # 去重账本保留期
    "oil_page":            _env("OIL_PAGE", "http://www.qiyoujiage.com/neimenggu.shtml"),
    "weather_url":         _env("WEATHER_URL", ""),               # 留空用内置呼和浩特 URL
    "voice_enabled":       _env("VOICE_ENABLED", "1").lower() not in ("0", "false", "no"),
    "tts_model":           _env("TTS_MODEL", "deepgram/flux-tts:free"),
    "tts_voice":           _env("TTS_VOICE", "flux-alexis-en"),   # Flux 音色: flux-<name>-en
}
