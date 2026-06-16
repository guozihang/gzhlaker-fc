"""
论文自动处理系统 - 阿里云函数计算入口

定时触发配置:
  凌晨 1:00 → {"step": "download_extract"}   下载论文 + 抽取文本
  凌晨 2:00 → {"step": "summarize"}           大模型总结论文
  凌晨 3:00 → {"step": "send"}                发送到飞书终端

本地测试:
  python main.py download_extract
  python main.py summarize
  python main.py send
"""

# ============================================================
# 标准库
# ============================================================
import datetime
import json
import os
import re
import subprocess
import sys
import time

# ============================================================
# 第三方库
# ============================================================
import arxiv
import oss2
import pypdfium2
import requests
from openai import OpenAI
from cozepy import COZE_CN_BASE_URL, Coze, TokenAuth

# ============================================================
# 配置 - 全部从环境变量获取，无硬编码密钥
# ============================================================

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

# ---- Coze 配置 (可选) ----
COZE_CONFIG = {
    "api_token": _env("COZE_API_TOKEN", ""),
    "workflow_id": _env("COZE_WORKFLOW_ID", "7489008649470378025"),
}

# ---- 飞书配置 ----
FEISHU_CONFIG = {
    "app_id": _require_env("FEISHU_APP_ID"),
    "app_secret": _require_env("FEISHU_APP_SECRET"),
    "receive_id": _require_env("FEISHU_RECEIVE_ID"),
}

# ---- Telegram 配置 (可选) ----
TELEGRAM_CONFIG = {
    "bot_token": _env("TELEGRAM_BOT_TOKEN", ""),
    "chat_id": _env("TELEGRAM_CHAT_ID", ""),
}

# ---- 默认发送通道 ----
DEFAULT_CHANNELS = _env("SEND_CHANNELS", "feishu").split(",")

# ============================================================
# OSS 工具函数
# ============================================================

def _oss_client(internal=True):
    """创建 OSS Bucket 客户端。internal=True 使用内网端点。"""
    endpoint = OSS_CONFIG["endpoint_internal"] if internal else OSS_CONFIG["endpoint_accelerate"]
    auth = oss2.Auth(OSS_CONFIG["access_key"], OSS_CONFIG["secret_key"])
    return oss2.Bucket(auth, endpoint, OSS_CONFIG["bucket_name"])


def _oss_load_json(filename, default=None, internal=True):
    """从 OSS 读取 JSON 文件，失败时返回 default。"""
    if default is None:
        default = {}
    try:
        content = _oss_client(internal).get_object(filename).read()
        data = json.loads(content)
        print(f"✅ 从 OSS 读取 {filename} ({_describe(data)} 条记录)")
        return data
    except Exception as e:
        print(f"⚠️ 读取 {filename} 失败: {e}")
        return default


def _oss_load_text(filename, internal=True):
    """从 OSS 读取文本文件。"""
    try:
        content = _oss_client(internal).get_object(filename).read().decode("utf-8")
        print(f"✅ 从 OSS 读取 {filename}")
        return content
    except Exception as e:
        print(f"❌ 读取 {filename} 失败: {e}")
        return None


def _oss_save_json(filename, data, internal=True):
    """将数据保存为 JSON 到 OSS。"""
    try:
        _oss_client(internal).put_object(filename, json.dumps(data, ensure_ascii=False))
        print(f"✅ 上传 {filename} 到 OSS ({_describe(data)} 条记录)")
    except Exception as e:
        print(f"❌ 上传 {filename} 失败: {e}")


def _oss_file_exists(filename, internal=True):
    """检查文件是否存在于 OSS。"""
    return _oss_client(internal).object_exists(filename)


def _oss_upload_file(local_path, oss_path, internal=True):
    """上传本地文件到 OSS。"""
    try:
        with open(local_path, "rb") as f:
            _oss_client(internal).put_object(oss_path, f.read())
        print(f"✅ 上传文件到 OSS: {oss_path}")
    except Exception as e:
        print(f"⚠️ 上传文件到 OSS 失败 ({oss_path}): {e}")


def _describe(data):
    """描述数据大小，用于日志输出。"""
    if isinstance(data, (list, dict)):
        return len(data)
    return "?"


# ============================================================
# PDF 文本抽取
# ============================================================

def _extract_pdf_text(pdf_path, title):
    """从 PDF 文件中抽取文本内容。"""
    try:
        with open(pdf_path, "rb") as f:
            pdf_document = pypdfium2.PdfDocument(f, autoclose=True)
            text_parts = []
            for page in pdf_document:
                text_page = page.get_textpage()
                text_parts.append(text_page.get_text_range())
                text_page.close()
                page.close()
        text = "".join(text_parts)
        print(f"✅ 文本抽取成功: {title} ({len(text)} 字符)")
        return text
    except Exception as e:
        print(f"⚠️ 文本抽取失败 ({title}): {e}")
        return None


# ============================================================
# Step 1: 下载论文 + 抽取文本（凌晨 1:00）
# ============================================================

_SYSTEM_PROMPT = """
你是一位专业的学术论文分析助手，需要从用户提供的论文中精准提取以下结构化信息：
**信息提取要求：**
1. 任务 - 本文研究的核心课题
2. 推荐等级 - 本文推荐的等级，从⭐到⭐⭐⭐⭐⭐
3. 推荐理由 - 推荐本文的具体内容，本文的什么内容可以解决什么挑战，挑战会给出，直接说明来源的文章解决了连续手语识别的什么挑战，可以用文章提出的什么解决？
4. 阅读时间 - 预估完整阅读所需时间（单位：分钟）
5. 论文题目 - 完整标题（保留大小写格式）
6. 会议/期刊/时间 - 发表平台及年份
7. 科学问题 - 论文要解决的本质问题,是需要突破的点
8. 挑战 - 研究面临的主要困难
9. 动机 - 开展研究的出发点
10. 对现有工作的批判性分析 - 作者指出的前人工作不足
11. 贡献 - 本文的创新点
12. 方法 - 核心技术方案（200字内）
13. 数据集 - 使用的实验数据
14. 指标 - 评估方法及量化标准
15. 代码链接 - 开源代码地址（如无可留空）
16. 优势 - 相比基线方法的改进
17. 核心创新点 - 关键方法论突破
18. 研究目标 - 期望达成的最终目的

**输出规范：**
- 严格生成无表头的标准JSON格式
- 使用简体中文表述内容
- 键名严格保持给定中文项（如"任务"、"阅读时间"）
- 字段顺序必须与上述列表完全一致
- 值部分禁止包含Markdown格式
- 无额外说明文字，仅输出JSON对象
- 不输出 ```json xxx ``` 这样的内容
"""


def step1_download_and_extract():
    """
    Step 1: 从 arxiv 下载论文并抽取文本内容。
    定时触发: 凌晨 1:00
    """
    print("=" * 50)
    print("📥 Step 1: 下载论文 & 抽取文本")
    print("=" * 50)

    # 加载状态文件
    read_papers = _oss_load_json("read_papers.json", [])
    unread_papers = _oss_load_json("unread_papers.json", [])
    queries = _oss_load_json("keywords.json", [])
    extracted_texts = _oss_load_json("extracted_texts.json", {})

    pdf_dir = "/tmp/papers"  # 函数计算中 /tmp 是可写目录
    os.makedirs(pdf_dir, exist_ok=True)

    arxiv_client = arxiv.Client()

    for query in queries:
        print(f"\n🔍 搜索关键词: {query}")
        try:
            search = arxiv.Search(
                query=query,
                max_results=50,
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )

            for result in arxiv_client.results(search):
                # 过滤旧论文
                if result.published.strftime("%Y-%m-%d") < "2025-04-20":
                    continue

                # 生成安全的文件名
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_" for c in result.title
                )

                if safe_title in read_papers or safe_title in unread_papers:
                    print(f"  ⏭️ 已处理过: {result.title}")
                    continue

                pdf_oss_path = f"papers/{safe_title}.pdf"
                pdf_local_path = os.path.join(pdf_dir, f"{safe_title}.pdf")

                # --- 情况 A: PDF 已存在于 OSS ---
                if _oss_file_exists(pdf_oss_path):
                    print(f"  📄 PDF 已存 OSS: {safe_title}.pdf")

                    # 下载到本地进行文本抽取
                    try:
                        pdf_content = _oss_client().get_object(pdf_oss_path).read()
                        with open(pdf_local_path, "wb") as f:
                            f.write(pdf_content)
                    except Exception as e:
                        print(f"  ⚠️ 从 OSS 下载 PDF 失败: {e}")
                        continue

                    text = _extract_pdf_text(pdf_local_path, safe_title)
                    if text:
                        extracted_texts[safe_title] = text
                        unread_papers.append(safe_title)
                else:
                    # --- 情况 B: 需要从 arxiv 下载 ---
                    try:
                        result.download_pdf(dirpath=pdf_dir, filename=f"{safe_title}.pdf")
                        print(f"  ✅ 下载成功: {safe_title}.pdf")
                    except Exception as e:
                        print(f"  ⚠️ 下载失败 ({result.title}): {e}")
                        continue

                    # 抽取文本
                    text = _extract_pdf_text(pdf_local_path, safe_title)
                    if text:
                        extracted_texts[safe_title] = text

                    # 上传 PDF 到 OSS
                    _oss_upload_file(pdf_local_path, pdf_oss_path)
                    unread_papers.append(safe_title)

                # 清理本地临时文件
                if os.path.exists(pdf_local_path):
                    os.remove(pdf_local_path)

                # 每处理一篇就持久化（防止中断丢失进度）
                _oss_save_json("extracted_texts.json", extracted_texts)
                _oss_save_json("unread_papers.json", unread_papers)

        except StopIteration:
            print(f"  ❌ 关键词无结果: {query}")
        except Exception as e:
            print(f"  ❌ 处理关键词 '{query}' 出错: {e}")

    print(f"\n🏁 Step 1 完成: 共抽取 {len(extracted_texts)} 篇论文文本")


# ============================================================
# Step 2: 大模型总结论文（凌晨 2:00）
# ============================================================

def step2_summarize(enable_coze=True):
    """
    Step 2: 调用大模型 API 总结论文，可选 Coze 补充推荐等级。
    定时触发: 凌晨 2:00
    event: {"step": "summarize", "enable_coze": true/false}
    """
    print("=" * 50)
    print("🤖 Step 2: 大模型总结论文")
    print("=" * 50)

    # 加载数据
    extracted_texts = _oss_load_json("extracted_texts.json", {})
    read_papers = _oss_load_json("read_papers.json", [])
    processed_papers = _oss_load_json("processed_papers.json", {})
    challenge_text = _oss_load_text("challenge_text.txt")

    if challenge_text is None:
        print("❌ 缺少 challenge_text.txt，终止执行")
        return

    if not extracted_texts:
        print("❌ 没有待处理的论文文本，终止执行")
        return

    # 初始化 LLM 客户端
    llm_client = OpenAI(
        api_key=LLM_CONFIG["api_key"],
        base_url=LLM_CONFIG["base_url"],
    )

    items_list = list(extracted_texts.items())
    # 从倒数第300篇开始（处理最近的论文）
    start_index = max(0, len(items_list) - 300)

    print(f"📊 共 {len(items_list)} 篇论文，从第 {start_index} 篇开始处理\n")

    for paper_title, paper_text in items_list[start_index:]:
        # 跳过已处理的
        if paper_title in read_papers:
            print(f"⏭️ 已总结过: {paper_title}")
            continue

        try:
            user_prompt = f"""
【输入规范】
1. 输入内容：{paper_text}中的学术论文文本

【处理任务】
将目标论文的关键要素提取为结构化数据，具体要求：
- 严格遵循系统提示中定义的18个字段规范
- 保持与参考案例相同的键名顺序和字段类型
- 数值型字段保留原始格式（如阅读时间"35"分钟）
- 文本内容使用简体中文表述
- 无额外说明文字，仅输出JSON对象
- 不输出 ```json xxx ``` 这样的内容
- 推荐等级和推荐理由根据文章可解决 【相关挑战】 中哪个挑战给出，可能解决多个挑战，请全部给出，给出格式为 **文章的xxx，可解决 xxx 挑战**，多个挑战之间用逗号分隔

【相关挑战】
{challenge_text}

【输出要求】
生成符合以下标准的JSON对象：
✓ 无多余表头/说明文字
✓ 完全排除Markdown格式
✓ 字段顺序与系统定义完全一致
✓ 字符串值使用自然中文表述
✓ 空值字段保持空字符串
✓ 不输出 ```json xxx ``` 这样的内容
"""

            # 截断过长文本
            if len(user_prompt) > 100000:
                print(f"⚠️ 论文过长，截断处理: {paper_title}")
                user_prompt = user_prompt[:100000]

            # 调用 LLM（openrouter/auto 自动选择最优模型）
            create_kwargs = {
                "model": LLM_CONFIG["model"],
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
            }

            # 若使用 openrouter/auto 且配置了候选模型池，通过 plugins 限制
            if LLM_CONFIG["model"] == "openrouter/auto" and LLM_CONFIG["auto_allowed_models"]:
                try:
                    allowed = json.loads(LLM_CONFIG["auto_allowed_models"])
                    create_kwargs["plugins"] = [{"id": "auto-router", "allowed_models": allowed}]
                    print(f"  🎯 auto-router 候选: {allowed}")
                except json.JSONDecodeError:
                    print("  ⚠️ OPENROUTER_AUTO_ALLOWED_MODELS 格式无效，已忽略")

            response = llm_client.chat.completions.create(**create_kwargs)

            content = response.choices[0].message.content
            if content:
                result = json.loads(content)
                processed_papers[paper_title] = result
                read_papers.append(paper_title)
                print(f"✅ 总结完成: {paper_title}")

                # 持久化
                _oss_save_json("processed_papers.json", processed_papers)
                _oss_save_json("read_papers.json", read_papers)
            else:
                print(f"❌ LLM 返回空内容: {paper_title}")

            # API 限速
            time.sleep(3)

        except Exception as e:
            print(f"❌ 处理论文出错 ({paper_title}): {e}")

    # --- Step 2.5: 通过 Coze 补充推荐等级 (可选) ---
    if enable_coze and processed_papers:
        _add_recommendations_via_coze(processed_papers)
    elif not enable_coze:
        print("\n⏭️ 已跳过 Coze 推荐 (enable_coze=False)")

    print(f"\n🏁 Step 2 完成: 共总结 {len(processed_papers)} 篇论文")


def _add_recommendations_via_coze(processed_papers):
    """调用 Coze 工作流为论文补充推荐等级和推荐理由。"""
    if not COZE_CONFIG["api_token"]:
        print("\n⏭️ 未配置 COZE_API_TOKEN，跳过 Coze 推荐")
        return

    print("\n--- 补充推荐等级 (Coze) ---")

    coze = Coze(
        auth=TokenAuth(token=COZE_CONFIG["api_token"]),
        base_url=COZE_CN_BASE_URL,
    )

    updated = False
    for key in list(processed_papers.keys()):
        if "推荐等级" in processed_papers[key]:
            print(f"  ⏭️ 已有推荐: {key}")
            continue

        try:
            workflow = coze.workflows.runs.create(
                workflow_id=COZE_CONFIG["workflow_id"],
                parameters={
                    "data": json.dumps(processed_papers[key], ensure_ascii=False),
                    "title": key,
                },
            )
            data = json.loads(workflow.data).get("data", "")

            # 清理可能的 markdown 包裹
            if "```json\n" in data:
                data = data.replace("```json\n", "").replace("\n```", "")

            recommendation = json.loads(data)
            processed_papers[key]["推荐等级"] = recommendation.get("推荐等级", "")
            processed_papers[key]["推荐理由"] = recommendation.get("推荐理由", "")
            updated = True
            print(f"  ✅ 补充推荐: {key}")

        except Exception as e:
            print(f"  ⚠️ 补充推荐失败 ({key}): {e}")

    if updated:
        _oss_save_json("processed_papers.json", processed_papers)


# ============================================================
# Step 3: 多通道发送（凌晨 3:00）
# ============================================================

_HEADERS = [
    "推荐等级", "推荐理由", "任务", "阅读时间", "论文题目",
    "会议/期刊/时间", "科学问题", "挑战", "动机",
    "对现有工作的批判性分析", "贡献", "方法", "数据集",
    "指标", "代码链接", "优势", "核心创新点", "研究目标",
]


def step3_send(channels=None):
    """
    Step 3: 将处理完的论文通过多个通道发送。
    event: {"step": "send", "channels": ["feishu", "telegram"]}
    不指定 channels 则使用环境变量 SEND_CHANNELS。
    """
    if channels is None:
        channels = DEFAULT_CHANNELS

    print("=" * 50)
    print(f"📤 Step 3: 发送论文 → 通道: {channels}")
    print("=" * 50)

    processed_papers = _oss_load_json("processed_papers.json", {})
    upload_papers = _oss_load_json("upload_papers.json", [], internal=False)

    if not processed_papers:
        print("❌ 没有已处理的论文，终止执行")
        return

    new_papers = []
    for title, info in processed_papers.items():
        if title not in upload_papers:
            new_papers.append((title, info))
            upload_papers.append(title)

    if not new_papers:
        print("✅ 没有需要发送的新论文")
        return

    _oss_save_json("upload_papers.json", upload_papers, internal=False)

    for channel in channels:
        channel = channel.strip().lower()
        sender = _CHANNEL_SENDERS.get(channel)
        if sender is None:
            print(f"⚠️ 未知通道: {channel}，已跳过")
            continue

        print(f"\n--- 通过 {channel} 发送 ---")
        ok = 0
        for i, (title, info) in enumerate(new_papers):
            if i > 0 and i % 5 == 0:
                time.sleep(1)
            if sender(title, info):
                ok += 1
        print(f"  {channel}: {ok}/{len(new_papers)} 成功")

    print(f"\n🏁 Step 3 完成: {len(new_papers)} 篇 → {len(channels)} 个通道")


# ============================================================
# 通道发送器
# ============================================================

def _send_to_feishu(paper_title, paper_info):
    """飞书通道：格式化论文并发送。"""
    blocks = [[
        {"tag": "text", "text": "【论文处理时间】", "style": ["bold"]},
        {"tag": "text", "text": datetime.datetime.now().strftime("%Y-%m-%d-%H-%M")},
    ]]
    for h in _HEADERS:
        v = paper_info.get(h, "") or "无"
        blocks.append([
            {"tag": "text", "text": f"【{h}】", "style": ["bold"]},
            {"tag": "text", "text": str(v)},
        ])
    return _send_raw_feishu({"zh_cn": {"title": paper_title, "content": blocks}})


def _send_raw_feishu(data):
    """底层飞书 API 调用。"""
    try:
        token = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": FEISHU_CONFIG["app_id"], "app_secret": FEISHU_CONFIG["app_secret"]},
            timeout=10,
        ).json()["app_access_token"]
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "user_id"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": FEISHU_CONFIG["receive_id"],
                "msg_type": "post",
                "content": json.dumps(data, ensure_ascii=False),
            },
            timeout=10,
        )
        lid = resp.headers.get("X-Tt-Logid", "?")
        ok = resp.status_code == 200
        print(f"  {'✅' if ok else '❌'} 飞书 (LogId: {lid})")
        return ok
    except Exception as e:
        print(f"  ❌ 飞书异常: {e}")
        return False


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


_CHANNEL_SENDERS = {
    "feishu": _send_to_feishu,
    "telegram": _send_to_telegram,
}


# ============================================================
# 附: CCF 投稿截止提醒
# ============================================================

def step_ccf_check():
    """检查 CCF 会议投稿截止日期并发送提醒。"""
    print("=" * 50)
    print("📅 CCF 投稿截止提醒")
    print("=" * 50)

    result = subprocess.run(
        ["python", "-m", "ccfddl"],
        capture_output=True, text=True,
    )

    content_blocks = []
    for line in result.stdout.split("\n"):
        if "https://" not in line:
            continue
        line = line.replace(" ", "")
        line = line.replace("days", "天").replace("months", "月")
        parts = line.split("│")
        try:
            match = re.search(r"'ccf'\s*:\s*'([^']*)'", parts[3])
            if match and match.group(1) == "A":
                content_blocks.append([{
                    "tag": "text",
                    "text": f"【CCF-{match.group(1)}】【{parts[1]}】剩余：{parts[4]}",
                }])
        except (IndexError, AttributeError):
            continue

    if content_blocks:
        msg = {"zh_cn": {"title": "投稿时间提醒", "content": content_blocks}}
        _send_raw_feishu(msg)
        print(f"✅ 已发送 CCF 提醒，共 {len(content_blocks)} 个会议")
    else:
        print("✅ 无 A 类会议即将截止")


# ============================================================
# 统一入口 - 阿里云函数计算 handler
# ============================================================

# 步骤路由表: step → (func, 从 event 提取的额外参数)
_STEP_MAP = {
    "download_extract": (step1_download_and_extract, []),
    "summarize":        (step2_summarize,         ["enable_coze"]),
    "send":             (step3_send,              ["channels"]),
    "ccf_check":        (step_ccf_check,          []),
}


def handler(event, context=None):
    """
    阿里云函数计算统一入口。

    event["step"] 指定步骤:
      "download_extract"  下载论文 + 抽取文本
      "summarize"         大模型总结 (可选 enable_coze)
      "send"              多通道发送 (可选 channels)
      "ccf_check"         CCF 投稿截止提醒

    定时触发器示例:
      凌晨 1:00 → {"step": "download_extract"}
      凌晨 2:00 → {"step": "summarize", "enable_coze": true}
      凌晨 3:00 → {"step": "send", "channels": ["feishu", "telegram"]}
    """
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            event = {"step": event}

    step = event.get("step", "download_extract")

    print(f"🚀 步骤: {step}")
    print(f"📅 时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📦 Event: {json.dumps(event, ensure_ascii=False)}")

    if step not in _STEP_MAP:
        available = list(_STEP_MAP.keys())
        print(f"❌ 未知步骤: {step}，可用: {available}")
        return {"status": "error", "message": f"Unknown '{step}'. Available: {available}"}

    func, extra_keys = _STEP_MAP[step]
    kwargs = {k: event[k] for k in extra_keys if k in event}

    try:
        func(**kwargs)
        print(f"🏁 {step} 完成")
        return {"status": "success", "step": step}
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        print(f"💥 {step} 失败: {e}")
        return {"status": "error", "step": step, "message": str(e)}


# ============================================================
# 本地测试入口
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        step = sys.argv[1]
    else:
        step = "download_extract"

    required = ["OSS_ACCESS_KEY", "OSS_SECRET_KEY", "OPENROUTER_API_KEY",
                "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_RECEIVE_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"⚠️ 未设置: {missing}")
        print("  在 FC 控制台配置环境变量，或本地 export 后运行\n")

    handler({"step": step})
