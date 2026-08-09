"""
Telegram Bot webhook — PDF 上传、关键词管理。
由 main.handler 在 HTTP 请求时延迟导入，定时器冷启动完全不加载此模块。
"""
import base64
import io
import json
import os
import sys
import traceback

import requests
from flask import Flask, request, jsonify

from oss_utils import (
    _oss_load_json, _oss_save_json, _oss_file_exists,
    _oss_upload_file, _safe_title,
)
from pdf_utils import _extract_pdf_text
from channels import _send_telegram_message
from config import TELEGRAM_CONFIG


# ============================================================
# Flask 应用
# ============================================================
app = Flask(__name__)

TELEGRAM_BOT_TOKEN = TELEGRAM_CONFIG["bot_token"]
TELEGRAM_CHAT_ID = TELEGRAM_CONFIG["chat_id"]

DEFAULT_KEYWORDS = [
    '"sign language"',
    '"video recognition"',
    '"video understanding"',
    '"action recognition"',
    '"speech recognition"',
    '"sequence modeling"',
    '"representation learning"',
    '"retrieval"',
]


# ============================================================
# WSGI 转换适配器 — FC3 HTTP 事件 ↔ Flask WSGI
# ============================================================

def _fc_event_to_wsgi(event):
    """将 FC3 HTTP 触发器事件转换为 WSGI environ dict。"""
    ctx = event.get("requestContext", {})
    http = ctx.get("http", {})
    method = http.get("method", "GET")
    path = http.get("path") or event.get("rawPath", "/")

    headers = event.get("headers", {}) or {}
    query_params = event.get("queryParameters", {}) or {}

    if query_params:
        qs_parts = []
        for k, v in query_params.items():
            if isinstance(v, list):
                for vi in v:
                    qs_parts.append(f"{k}={vi}")
            else:
                qs_parts.append(f"{k}={v}")
        qs = "&".join(qs_parts)
    else:
        raw_qs = event.get("rawQueryString", "")
        qs = raw_qs.lstrip("?") if raw_qs else ""

    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(body)
    elif isinstance(body, str):
        raw_body = body.encode("utf-8")
    else:
        raw_body = body

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SCRIPT_NAME": "",
        "QUERY_STRING": qs,
        "SERVER_NAME": headers.get("Host", "fc.aliyuncs.com"),
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": http.get("protocol", "HTTP/1.1"),
        "REMOTE_ADDR": http.get("sourceIp", ""),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https",
        "wsgi.input": io.BytesIO(raw_body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(raw_body)),
    }

    for k, v in headers.items():
        lk = k.lower()
        if lk == "content-type":
            environ["CONTENT_TYPE"] = v
        elif lk == "content-length":
            environ["CONTENT_LENGTH"] = v
        else:
            key = "HTTP_" + lk.upper().replace("-", "_")
            environ[key] = v

    return environ


def handle_http_event(event):
    """FC3 HTTP 入口：将事件转为 WSGI 请求，经 Flask 处理后返回 FC 响应格式。"""
    environ = _fc_event_to_wsgi(event)

    status_box = []
    header_box = []

    def start_response(status, headers, exc_info=None):
        status_box.append(status)
        header_box.extend(headers)

    try:
        app_iter = app.wsgi_app(environ, start_response)
        body_chunks = []
        for chunk in app_iter:
            if isinstance(chunk, str):
                body_chunks.append(chunk.encode("utf-8"))
            else:
                body_chunks.append(chunk)
        body = b"".join(body_chunks)
        status_code = int(status_box[0].split(" ", 1)[0])
    except Exception:
        traceback.print_exc()
        return {
            "statusCode": 500,
            "headers": {"content-type": "text/plain; charset=utf-8"},
            "body": "Internal Server Error",
            "isBase64Encoded": False,
        }

    out_headers = {}
    for k, v in header_box:
        lk = k.lower()
        if lk in ("transfer-encoding", "connection", "content-length"):
            continue
        out_headers[k] = v

    try:
        return {
            "statusCode": status_code,
            "headers": out_headers,
            "body": body.decode("utf-8"),
            "isBase64Encoded": False,
        }
    except UnicodeDecodeError:
        return {
            "statusCode": status_code,
            "headers": out_headers,
            "body": base64.b64encode(body).decode("ascii"),
            "isBase64Encoded": True,
        }


# ============================================================
# Bot 命令处理
# ============================================================

def _handle_text(chat_id, text):
    """处理 Telegram Bot 文本命令。"""
    cmd = text.strip().lower()

    if cmd in ("/help", "/start", "help"):
        _send_telegram_message(chat_id,
            "📋 可用命令：\n\n"
            "/keywords — 查看当前关键词\n"
            "/add <关键词> — 添加关键词\n"
            "/delete <关键词> — 删除关键词\n"
            "/num — 查看队列篇数\n"
            "\n直接发送 PDF 文件即可上传并加入待读队列。"
        )

    elif cmd == "/keywords":
        keywords = _oss_load_json("keywords.json", DEFAULT_KEYWORDS)
        lines = [f"• {kw}" for kw in keywords]
        _send_telegram_message(chat_id, "📌 当前关键词：\n\n" + "\n".join(lines))

    elif cmd.startswith("/add "):
        kw = text.split("/add ", 1)[1].strip()
        if not kw:
            _send_telegram_message(chat_id, "用法: /add <关键词>")
            return
        keywords = _oss_load_json("keywords.json", DEFAULT_KEYWORDS)
        formatted = f'"{kw}"'
        if formatted in keywords:
            _send_telegram_message(chat_id, f"⚠️ 「{kw}」已存在")
        else:
            keywords.append(formatted)
            _oss_save_json("keywords.json", keywords)
            _send_telegram_message(chat_id, f"✅ 已添加: {formatted}")

    elif cmd.startswith("/delete ") or cmd.startswith("/remove "):
        kw = text.split(" ", 1)[1].strip()
        if not kw:
            _send_telegram_message(chat_id, "用法: /delete <关键词>")
            return
        keywords = _oss_load_json("keywords.json", DEFAULT_KEYWORDS)
        formatted = f'"{kw}"'
        if formatted in keywords:
            keywords.remove(formatted)
            _oss_save_json("keywords.json", keywords)
            _send_telegram_message(chat_id, f"✅ 已删除: {formatted}")
        else:
            _send_telegram_message(chat_id, f"⚠️ 未找到: {formatted}")

    elif cmd == "/num":
        unread_queue = _oss_load_json("unread_queue.json", [])
        _send_telegram_message(chat_id, f"📬 待总结队列: {len(unread_queue)} 篇")

    else:
        _send_telegram_message(chat_id,
            "发送 PDF 文件即可上传，或输入 /help 查看命令。"
        )


# ============================================================
# PDF 上传处理
# ============================================================

def _handle_pdf(chat_id, document):
    """处理 PDF 上传：下载 → OSS → 抽取文本 → 入队。"""
    file_name = document.get("file_name", "unknown.pdf")
    file_id = document.get("file_id")
    mime_type = document.get("mime_type", "")

    if not file_name.lower().endswith(".pdf") and mime_type != "application/pdf":
        _send_telegram_message(chat_id, f"⚠️ 仅支持 PDF，当前: {file_name}")
        return

    if not file_id or not TELEGRAM_BOT_TOKEN:
        _send_telegram_message(chat_id, "❌ 服务配置错误")
        return

    safe_title = _safe_title(file_name)

    if _oss_file_exists(f"extracted_texts/{safe_title}.json"):
        _send_telegram_message(chat_id, f"⚠️ 「{file_name}」已在队列中")
        return

    # 1. 获取 Telegram 文件下载路径
    tg_resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
        params={"file_id": file_id}, timeout=10,
    ).json()
    if not tg_resp.get("ok"):
        _send_telegram_message(chat_id, f"❌ 获取文件失败: {tg_resp.get('description', '?')}")
        return

    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_resp['result']['file_path']}"

    # 2. 下载
    pdf_resp = requests.get(download_url, timeout=120)
    if pdf_resp.status_code != 200:
        _send_telegram_message(chat_id, f"❌ 下载失败 (HTTP {pdf_resp.status_code})")
        return

    pdf_dir = "/tmp/papers"
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_local = os.path.join(pdf_dir, f"{safe_title}.pdf")
    with open(pdf_local, "wb") as f:
        f.write(pdf_resp.content)

    # 3. 上传 OSS
    pdf_oss = f"papers/{safe_title}.pdf"
    if not _oss_file_exists(pdf_oss):
        _oss_upload_file(pdf_local, pdf_oss)

    # 4. 抽取文本
    text = _extract_pdf_text(pdf_local, safe_title)
    if text:
        _oss_save_json(f"extracted_texts/{safe_title}.json", {"title": safe_title, "text": text})

    # 5. 入队
    unread_queue = _oss_load_json("unread_queue.json", [])
    if safe_title not in unread_queue:
        unread_queue.append(safe_title)
        _oss_save_json("unread_queue.json", unread_queue)

    if os.path.exists(pdf_local):
        os.remove(pdf_local)

    _send_telegram_message(chat_id,
        f"✅ 上传完成！\n\n"
        f"📄 {file_name}\n"
        f"📝 {len(text) if text else 0} 字符\n"
        f"📬 队列共 {len(unread_queue)} 篇"
    )
    print(f"✅ Telegram PDF 上传: {file_name} (chat_id={chat_id})")


# ============================================================
# 路由: Telegram Webhook
# ============================================================

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    """Telegram Bot webhook 入口。"""
    try:
        update = request.get_json(force=True)
        print(f"📨 Telegram: {json.dumps(update, ensure_ascii=False)[:300]}")

        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if not chat_id:
            return jsonify({"ok": True})

        # 仅响应授权用户
        if str(chat_id) != TELEGRAM_CHAT_ID:
            print(f"⛔ 未授权 chat_id: {chat_id}")
            return jsonify({"ok": True})

        # 文本命令
        text = message.get("text", "")
        if text:
            _handle_text(chat_id, text)
            return jsonify({"ok": True})

        # PDF 上传
        document = message.get("document")
        if document:
            _handle_pdf(chat_id, document)
            return jsonify({"ok": True})

        # 非文本非文件
        _send_telegram_message(chat_id, "发送 PDF 文件即可上传，或输入 /help 查看命令。")

    except Exception as e:
        traceback.print_exc()
    return jsonify({"ok": True})


# ============================================================
# 本地启动入口
# ============================================================

if __name__ == "__main__":
    print("🚀 本地启动 Flask 开发服务器...")
    app.run(debug=True, host="0.0.0.0", port=9000)
