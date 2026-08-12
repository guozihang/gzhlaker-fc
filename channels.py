"""
通道发送器 — Telegram 消息推送。
"""

import datetime
import time

import requests

from config import TELEGRAM_CONFIG
from oss_utils import _oss_load_json, _oss_save_json


_HEADERS = [
    "推荐等级", "推荐理由", "任务", "阅读时间", "论文题目",
    "会议/期刊/时间", "科学问题", "挑战", "动机",
    "对现有工作的批判性分析", "贡献", "方法", "数据集",
    "指标", "代码链接", "优势", "核心创新点", "研究目标", "英文摘要",
]


def _send_to_telegram(paper_title, paper_info):
    """Telegram 通道：格式化论文并发送。"""
    bot = TELEGRAM_CONFIG["bot_token"]
    cid = TELEGRAM_CONFIG["chat_id"]
    if not bot or not cid:
        print("  ⚠️ Telegram 未配置 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    text = _fmt_telegram(paper_title, paper_info)

    def _post(txt, html=True):
        return requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": cid, "text": txt, "parse_mode": "HTML" if html else None,
                  "disable_web_page_preview": False},
            timeout=10,
        ).json()

    r = _post(text)
    if r.get("ok"):
        print(f"  ✅ Telegram: {paper_title}")
        return True
    # HTML 解析失败则纯文本重试
    if "parse" in str(r.get("description", "")).lower():
        r2 = _post(_fmt_telegram(paper_title, paper_info, html=False), html=False)
        if r2.get("ok"):
            print(f"  ✅ Telegram (纯文本): {paper_title}")
            return True
    print(f"  ❌ Telegram: {r.get('description', '?')}")
    return False


def _fmt_telegram(title, info, html=True):
    t = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M")
    b = "<b>" if html else ""
    e = "</b>" if html else ""
    lines = [f"📄 {b}{title}{e}", f"⏰ {b}处理时间:{e} {t}", ""]
    for h in _HEADERS:
        v = info.get(h, "") or "无"
        lines.append(f"{b}【{h}】{e} {v}")
    text = "\n".join(lines)
    return text[:4000] + ("\n\n... (截断)" if len(text) > 4000 else "")


def _send_telegram_raw(text, parse_mode="Markdown", prefix="", queue_delete=False):
    """发送任意文本到 Telegram，支持自动分段和 Markdown/纯文本回退。

    Args:
        text: 要发送的文本
        parse_mode: Telegram parse_mode
        prefix: 多段时的前缀（如 "📊 周报"）
        queue_delete: True 时把消息 ID 加入待删除队列，可被 /clear 清理
    """
    bot = TELEGRAM_CONFIG["bot_token"]
    cid = TELEGRAM_CONFIG["chat_id"]
    if not bot or not cid:
        print("  ⚠️ Telegram 未配置 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    max_len = 4000

    # 按 max_len 分段，优先在段落边界断开
    parts = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    parts.append(remaining)

    total = len(parts)
    ok = 0
    for i, part in enumerate(parts):
        if total > 1 and prefix:
            body = f"{prefix} ({i+1}/{total})\n\n{part}"
        elif prefix:
            body = f"{prefix}\n\n{part}"
        else:
            body = part

        r = requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": cid, "text": body, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=10,
        ).json()

        if r.get("ok"):
            ok += 1
            if queue_delete:
                _queue_delete(cid, r["result"]["message_id"])
        elif parse_mode and "parse" in str(r.get("description", "")).lower():
            r2 = requests.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": cid, "text": body, "disable_web_page_preview": True},
                timeout=10,
            ).json()
            if r2.get("ok"):
                ok += 1
                if queue_delete:
                    _queue_delete(cid, r2["result"]["message_id"])
            else:
                print(f"  ❌ Telegram 第{i+1}段发送失败: {r2.get('description', '?')}")
        else:
            print(f"  ❌ Telegram 第{i+1}段发送失败: {r.get('description', '?')}")

    print(f"  Telegram: {ok}/{total} 段成功")
    return ok == total


def _send_telegram_message(chat_id, text, parse_mode=None):
    """发送文本到指定 Telegram 会话，自动在 10 分钟后删除。"""
    bot = TELEGRAM_CONFIG["bot_token"]
    if not bot:
        print("  ⚠️ Telegram 未配置 (TELEGRAM_BOT_TOKEN)")
        return False

    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    r = requests.post(
        f"https://api.telegram.org/bot{bot}/sendMessage",
        json=payload,
        timeout=10,
    ).json()

    # Markdown 解析失败则纯文本重试
    if not r.get("ok") and parse_mode and "parse" in str(r.get("description", "")).lower():
        r = requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        ).json()

    if r.get("ok"):
        msg_id = r["result"]["message_id"]
        _queue_delete(chat_id, msg_id)
        return True
    print(f"  ❌ Telegram 回复失败: {r.get('description', '?')}")
    return False


def _queue_delete(chat_id, message_id, delay_seconds=600):
    """将消息 ID 加入待删除队列（OSS），10 分钟后由定时器清理。"""
    pending = _oss_load_json("tg_pending_delete.json", [])
    pending.append({
        "chat_id": str(chat_id),
        "message_id": message_id,
        "delete_after": time.time() + delay_seconds,
    })
    _oss_save_json("tg_pending_delete.json", pending)
