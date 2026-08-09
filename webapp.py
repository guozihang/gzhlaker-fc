"""
Flask API 应用 — 论文上传、关键词管理、静态文件服务。
由 main.handler 在 HTTP 请求时延迟导入，定时器冷启动完全不加载此模块。
"""
import base64
import io
import json
import os
import sys
import traceback

import requests
from flask import Flask, request, jsonify, render_template_string, send_from_directory

from oss_utils import (
    _oss_load_json, _oss_save_json, _oss_file_exists,
    _oss_upload_file, _safe_title,
)
from pdf_utils import _extract_pdf_text
from channels import _send_telegram_message


# ============================================================
# Flask 应用
# ============================================================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(_BASE_DIR, "static"), static_url_path="")


# CORS — 用 after_request 替代 flask-cors，减少依赖
@app.after_request
def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ============================================================
# 环境变量
# ============================================================
MANAGE_PASSWORD = os.environ.get("MANAGE_PASSWORD", "your_secure_password")

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
    # requestContext.http.path 是已解码路径，rawPath 是 URL 编码的
    path = http.get("path") or event.get("rawPath", "/")

    headers = event.get("headers", {}) or {}
    query_params = event.get("queryParameters", {}) or {}

    # 构建 query string
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

    # Body: FC 可能 base64 编码
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

    # 收集响应头，丢弃 hop-by-hop 头
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
# 路由: 论文上传
# ============================================================

@app.route("/upload_paper", methods=["GET", "POST", "PUT", "DELETE"])
def upload_paper():
    """上传论文 PDF，自动提取文本并加入 unread_queue 队列。"""
    try:
        pdf_file = request.files.get("pdf_file")
        if not pdf_file:
            return jsonify({"error": "缺少 pdf_file 字段"}), 400

        file_name = pdf_file.filename
        safe_title = _safe_title(file_name)
        pdf_dir = "/tmp/papers"
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_local_path = os.path.join(pdf_dir, f"{safe_title}.pdf")
        pdf_oss_path = f"papers/{safe_title}.pdf"

        # 读取文件内容
        file_content = pdf_file.read()

        # 查重：是否已在队列或已提取文本
        unread_queue = _oss_load_json("unread_queue.json", [])
        if safe_title in unread_queue or _oss_file_exists(f"extracted_texts/{safe_title}.json"):
            return jsonify({"message": "论文已存在", "file_name": file_name})

        # 保存到本地 /tmp
        with open(pdf_local_path, "wb") as f:
            f.write(file_content)

        # 上传 PDF 到 OSS（如不存在）
        if not _oss_file_exists(pdf_oss_path):
            _oss_upload_file(pdf_local_path, pdf_oss_path)

        # 提取文本
        text = _extract_pdf_text(pdf_local_path, safe_title)
        if text:
            _oss_save_json(
                f"extracted_texts/{safe_title}.json",
                {"title": safe_title, "text": text},
            )

        # 加入队列
        unread_queue.append(safe_title)
        _oss_save_json("unread_queue.json", unread_queue)

        # 清理临时文件
        if os.path.exists(pdf_local_path):
            os.remove(pdf_local_path)

        return jsonify({"message": "论文上传成功", "file_name": file_name})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"服务器内部错误: {str(e)}"}), 500


# ============================================================
# 路由: 关键词管理 Web UI
# ============================================================

KEYWORDS_MANAGER_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>关键词管理 - Element UI</title>
    <link rel="stylesheet" href="https://unpkg.com/element-ui/lib/theme-chalk/index.css">
    <style>
        body { margin: 0; padding: 20px; background-color: #f0f2f5; }
        .el-card { max-width: 800px; margin: 0 auto; }
        .keyword-item { display: flex; align-items: center; margin-bottom: 12px; }
        .keyword-item .el-input { flex: 1; margin-right: 10px; }
        .action-buttons { margin-top: 20px; display: flex; justify-content: space-between; }
        .password-section { margin-top: 24px; padding-top: 24px; border-top: 1px solid #ebeef5; }
    </style>
</head>
<body>
    <div id="app">
        <el-card>
            <template #header>
                <div class="card-header"><span>关键词管理</span></div>
            </template>
            <div id="keywords-container">
                <div class="keyword-item" v-for="(kw, idx) in localKeywords" :key="idx">
                    <el-input v-model="localKeywords[idx]" placeholder="请输入关键词"></el-input>
                    <el-button type="danger" plain icon="el-icon-delete" @click="removeKeyword(idx)"></el-button>
                </div>
            </div>
            <div class="action-buttons">
                <el-button type="primary" icon="el-icon-plus" @click="addKeyword">添加关键词</el-button>
            </div>
            <div class="password-section">
                <h3 style="margin-bottom: 16px;">更新关键词</h3>
                <el-form :inline="true">
                    <el-form-item label="密码">
                        <el-input v-model="password" type="password" placeholder="请输入密码" show-password></el-input>
                    </el-form-item>
                    <el-form-item>
                        <el-button type="success" @click="saveKeywords" :loading="saving">保存更改</el-button>
                    </el-form-item>
                </el-form>
            </div>
        </el-card>
    </div>
    <script src="https://unpkg.com/vue/dist/vue.js"></script>
    <script src="https://unpkg.com/element-ui/lib/index.js"></script>
    <script>
        new Vue({
            el: '#app',
            data() {
                return {
                    localKeywords: {{ keywords_json | safe }},
                    password: '',
                    saving: false
                };
            },
            methods: {
                addKeyword() { this.localKeywords.push(''); },
                removeKeyword(index) {
                    this.$confirm('确定要移除这个关键词吗？', '提示', {
                        confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning'
                    }).then(() => {
                        this.localKeywords.splice(index, 1);
                        this.$message({ type: 'success', message: '移除成功' });
                    }).catch(() => {});
                },
                saveKeywords() {
                    if (!this.password) { this.$message.warning('请输入密码'); return; }
                    var keywords = this.localKeywords.filter(function(k) { return k.trim() !== ''; });
                    if (keywords.length === 0) { this.$message.warning('关键词列表不能为空'); return; }
                    this.saving = true;
                    fetch('/update_keywords', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ keywords: keywords, password: this.password })
                    }).then(function(r) { return r.json(); }).then((data) => {
                        this.saving = false;
                        if (data.success) {
                            this.$message({ type: 'success', message: '关键词更新成功！' });
                            this.password = '';
                        } else {
                            this.$message.error('错误: ' + data.message);
                        }
                    }).catch((error) => {
                        this.saving = false;
                        this.$message.error('保存失败: 网络错误');
                        console.error('Error:', error);
                    });
                }
            }
        });
    </script>
</body>
</html>
"""


@app.route("/keywords_manager")
def keywords_manager():
    """关键词管理 Web UI。"""
    keywords = _oss_load_json("keywords.json", DEFAULT_KEYWORDS)
    return render_template_string(
        KEYWORDS_MANAGER_HTML,
        keywords_json=json.dumps(keywords, ensure_ascii=False),
    )


@app.route("/update_keywords", methods=["POST"])
def update_keywords():
    """更新关键词（需管理密码）。"""
    try:
        data = request.get_json(force=True)
        keywords = data.get("keywords", [])
        password = data.get("password", "")

        if password != MANAGE_PASSWORD:
            return jsonify({"success": False, "message": "密码错误"})

        if not isinstance(keywords, list):
            return jsonify({"success": False, "message": "关键词必须是数组格式"})

        if _oss_save_json("keywords.json", keywords):
            return jsonify({"success": True, "message": "关键词更新成功"})
        else:
            return jsonify({"success": False, "message": "保存失败，请重试"})
    except Exception as e:
        return jsonify({"success": False, "message": f"服务器内部错误: {str(e)}"}), 500


@app.route("/get_keywords")
def get_keywords():
    """获取当前关键词列表。"""
    keywords = _oss_load_json("keywords.json", DEFAULT_KEYWORDS)
    return jsonify({"keywords": keywords})


# ============================================================
# 路由: Telegram Bot Webhook — 接收 PDF 上传
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    """Telegram Bot webhook：接收用户上传的 PDF，自动处理并加入队列。"""
    try:
        update = request.get_json(force=True)
        print(f"📨 Telegram webhook: {json.dumps(update, ensure_ascii=False)[:500]}")

        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if not chat_id:
            return jsonify({"ok": True})

        document = message.get("document")
        if not document:
            _send_telegram_message(chat_id, "请直接发送 PDF 文件，我会自动提取文本并加入待读队列。")
            return jsonify({"ok": True})

        file_name = document.get("file_name", "unknown.pdf")
        mime_type = document.get("mime_type", "")
        file_id = document.get("file_id")

        # 只接受 PDF
        if not file_name.lower().endswith(".pdf") and mime_type != "application/pdf":
            _send_telegram_message(chat_id, f"⚠️ 仅支持 PDF 文件，当前文件: {file_name}（{mime_type}）")
            return jsonify({"ok": True})

        if not file_id or not TELEGRAM_BOT_TOKEN:
            _send_telegram_message(chat_id, "❌ 服务配置错误，请联系管理员。")
            return jsonify({"ok": True})

        safe_title = _safe_title(file_name)

        # 查重
        if _oss_file_exists(f"extracted_texts/{safe_title}.json"):
            _send_telegram_message(chat_id, f"⚠️ 「{file_name}」已存在于队列中，无需重复上传。")
            return jsonify({"ok": True})

        # Step 1: 通过 Telegram API 获取文件下载路径
        tg_resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10,
        ).json()

        if not tg_resp.get("ok"):
            _send_telegram_message(chat_id, f"❌ 获取文件信息失败: {tg_resp.get('description', '未知错误')}")
            return jsonify({"ok": True})

        file_path = tg_resp["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

        # Step 2: 下载 PDF
        pdf_resp = requests.get(download_url, timeout=120)
        if pdf_resp.status_code != 200:
            _send_telegram_message(chat_id, f"❌ 下载文件失败 (HTTP {pdf_resp.status_code})")
            return jsonify({"ok": True})

        pdf_dir = "/tmp/papers"
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_local_path = os.path.join(pdf_dir, f"{safe_title}.pdf")
        with open(pdf_local_path, "wb") as f:
            f.write(pdf_resp.content)

        # Step 3: 上传到 OSS
        pdf_oss_path = f"papers/{safe_title}.pdf"
        if not _oss_file_exists(pdf_oss_path):
            _oss_upload_file(pdf_local_path, pdf_oss_path)

        # Step 4: 抽取文本
        text = _extract_pdf_text(pdf_local_path, safe_title)
        if text:
            _oss_save_json(
                f"extracted_texts/{safe_title}.json",
                {"title": safe_title, "text": text},
            )

        # Step 5: 加入队列
        unread_queue = _oss_load_json("unread_queue.json", [])
        if safe_title not in unread_queue:
            unread_queue.append(safe_title)
            _oss_save_json("unread_queue.json", unread_queue)

        # 清理
        if os.path.exists(pdf_local_path):
            os.remove(pdf_local_path)

        # Step 6: 回复用户
        _send_telegram_message(
            chat_id,
            f"✅ 上传完成！\n\n📄 论文: {file_name}\n📝 已提取 {len(text) if text else 0} 字符\n📬 已加入待总结队列（当前共 {len(unread_queue)} 篇）",
        )
        print(f"✅ Telegram 上传处理完成: {file_name} (chat_id={chat_id})")

    except Exception as e:
        traceback.print_exc()
        chat_id = (update.get("message", {}).get("chat", {}).get("id")) if 'update' in dir() else None
        if chat_id:
            _send_telegram_message(chat_id, f"❌ 处理失败: {e}")
    return jsonify({"ok": True})


# ============================================================
# 路由: 静态文件
# ============================================================

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def index(path):
    """静态文件服务。"""
    static_dir = os.path.join(_BASE_DIR, "static")
    if path and os.path.exists(os.path.join(static_dir, path)):
        try:
            return send_from_directory(static_dir, path)
        except Exception:
            pass
    # 兜底：返回 index.html（SPA 兼容）
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(static_dir, "index.html")
    return jsonify({"message": "gzhlaker FC API", "routes": ["/upload_paper", "/keywords_manager", "/get_keywords", "/telegram_webhook"]})


# ============================================================
# 本地启动入口
# ============================================================

if __name__ == "__main__":
    print("🚀 本地启动 Flask 开发服务器...")
    app.run(debug=True, host="0.0.0.0", port=9000)
