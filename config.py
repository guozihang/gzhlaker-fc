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
    "phone": _require_env("DIDA_PHONE"),
    "password": _require_env("DIDA_PASSWORD"),
    "breakdown_tag": "拆解",
}
