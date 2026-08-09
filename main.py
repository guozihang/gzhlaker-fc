"""
论文自动处理系统 - 阿里云函数计算入口

定时触发配置 (Asia/Shanghai):
  00:00/08:00/16:00 → {"step": "download_extract"}   下载论文 + 抽取文本
  02:00/10:00/18:00 → {"step": "summarize"}           大模型总结 + Telegram 推送
  周一 08:00        → {"step": "weekly_summary"}      每周总结
  (send / ccf_check 手动触发)

本地测试:
  python main.py download_extract
  python main.py summarize
  python main.py send
  python main.py weekly_summary
"""


# ============================================================
# 标准库
# ============================================================
import datetime
import json
import os
import random
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

# ---- 滴答清单配置 ----
DIDA_CONFIG = {
    "phone": _require_env("DIDA_PHONE"),
    "password": _require_env("DIDA_PASSWORD"),
}

# ============================================================
# OSS 工具函数
# ============================================================

def _oss_client(internal=True):
    """创建 OSS Bucket 客户端。internal=True 使用内网端点。"""
    endpoint = OSS_CONFIG["endpoint_internal"] if internal else OSS_CONFIG["endpoint_accelerate"]
    auth = oss2.Auth(OSS_CONFIG["access_key"], OSS_CONFIG["secret_key"])
    # connect_timeout: TCP 连接超时（FC 层 oss2 版本较旧，仅支持此参数）
    return oss2.Bucket(auth, endpoint, OSS_CONFIG["bucket_name"],
                       connect_timeout=15)


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


def _oss_save_json(filename, data, internal=True, retries=2):
    """将数据保存为 JSON 到 OSS，支持自动重试。"""
    body = json.dumps(data, ensure_ascii=False)
    last_err = None
    for attempt in range(retries + 1):
        try:
            _oss_client(internal).put_object(filename, body)
            if attempt > 0:
                print(f"  ↳ 重试成功")
            print(f"✅ 上传 {filename} 到 OSS ({_describe(data)} 条记录, {len(body)} 字节)")
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = (attempt + 1) * 3
                print(f"⚠️ 上传 {filename} 失败 (第{attempt+1}次): [{type(e).__name__}] {e}，{wait}s 后重试")
                time.sleep(wait)
    print(f"❌ 上传 {filename} 最终失败 (已重试{retries}次): [{type(last_err).__name__}] {last_err}")


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

    每篇论文的文本存入独立文件 extracted_texts/{safe_title}.json，避免单文件过大。
    """
    print("=" * 50)
    print("📥 Step 1: 下载论文 & 抽取文本")
    print("=" * 50)

    # 加载队列（同时用作 O(1) 去重集）和关键词
    unread_queue = _oss_load_json("unread_queue.json", [])
    queue_set = set(unread_queue)
    queries = _oss_load_json("keywords.json", [])

    # ── 恢复已有数据 ──
    # 扫描 extracted_texts/ 和 processed/，将"有文本但未处理"的论文加入队列。
    # PDF 可能已被 OSS 过期策略删除，但有文本就能总结。
    try:
        extracted_titles = set()
        for obj in oss2.ObjectIterator(_oss_client(), prefix="extracted_texts/"):
            if obj.key.endswith(".json"):
                extracted_titles.add(obj.key[len("extracted_texts/"):-len(".json")])

        processed_titles = set()
        for obj in oss2.ObjectIterator(_oss_client(), prefix="processed/"):
            if obj.key.endswith(".json"):
                processed_titles.add(obj.key[len("processed/"):-len(".json")])

        # 有提取文本 且 未处理 且 不在队列中 → 入队
        missing = extracted_titles - processed_titles - queue_set
        for title in missing:
            unread_queue.append(title)
            queue_set.add(title)

        if missing:
            print(f"📦 从已有文件恢复 {len(missing)} 篇到队列")
            _oss_save_json("unread_queue.json", unread_queue)
    except Exception as e:
        print(f"⚠️ 恢复旧数据失败: {e}")

    # 时间预算：云函数超时 3000s，预留 120s 安全边际
    _DEADLINE = time.monotonic() + 2880  # 48 分钟后强制停止新任务
    _MIN_REMAINING = 60  # 剩余不足 60s 时停止，留时间做最后的 OSS 保存
    _SAVE_EVERY = 5  # 每 N 篇持久化一次
    _new_since_save = 0

    pdf_dir = "/tmp/papers"  # 函数计算中 /tmp 是可写目录
    os.makedirs(pdf_dir, exist_ok=True)

    # delay_seconds: 请求间隔（默认3秒，arXiv 限流严格，加大到15秒）
    # num_retries: 失败重试次数（减少重试，避免越重试越被限流）
    arxiv_client = arxiv.Client(
        page_size=50,
        delay_seconds=15.0,
        num_retries=2,
    )

    # arXiv 对不同关键词的搜索之间至少间隔 60 秒，避免触发 429
    _QUERY_COOLDOWN = 60

    for idx, query in enumerate(queries):
        # 第一个关键词不需要等待，后续关键词之间等待
        if idx > 0:
            print(f"\n⏳ 等待 {_QUERY_COOLDOWN}s 后搜索下一个关键词...")
            time.sleep(_QUERY_COOLDOWN)

        print(f"\n🔍 搜索关键词: {query}")
        try:
            search = arxiv.Search(
                query=query,
                max_results=50,
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )

            for result in arxiv_client.results(search):
                # ---- 时间预算检查：剩余时间不足则优雅退出 ----
                remaining = _DEADLINE - time.monotonic()
                if remaining < _MIN_REMAINING:
                    print(f"  ⏰ 剩余时间 {remaining:.0f}s 不足 {_MIN_REMAINING}s，停止处理新论文")
                    break

                # 过滤旧论文
                if result.published.strftime("%Y-%m-%d") < "2025-04-20":
                    continue

                # 生成安全的文件名（也用作论文唯一 ID）
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_" for c in result.title
                )

                # O(1) set 查重：在队列中或原始文件已存在则跳过
                if safe_title in queue_set or _oss_file_exists(f"extracted_texts/{safe_title}.json"):
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
                        _oss_save_json(f"extracted_texts/{safe_title}.json",
                                       {"title": safe_title, "text": text})
                        queue_set.add(safe_title)
                        unread_queue.append(safe_title)
                else:
                    # --- 情况 B: 需要从 arxiv 下载 ---
                    try:
                        resp = requests.get(result.pdf_url, timeout=120)
                        resp.raise_for_status()
                        with open(pdf_local_path, "wb") as f:
                            f.write(resp.content)
                        print(f"  ✅ 下载成功: {safe_title}.pdf")
                    except Exception as e:
                        print(f"  ⚠️ 下载失败 ({result.title}): {e}")
                        continue

                    # 抽取文本
                    text = _extract_pdf_text(pdf_local_path, safe_title)
                    if text:
                        _oss_save_json(f"extracted_texts/{safe_title}.json",
                                       {"title": safe_title, "text": text})

                    # 上传 PDF 到 OSS
                    _oss_upload_file(pdf_local_path, pdf_oss_path)
                    queue_set.add(safe_title)
                    unread_queue.append(safe_title)

                # 清理本地临时文件
                if os.path.exists(pdf_local_path):
                    os.remove(pdf_local_path)

                _new_since_save += 1

                # 每 N 篇或时间紧张时持久化队列
                remaining = _DEADLINE - time.monotonic()
                if _new_since_save >= _SAVE_EVERY or remaining < 120:
                    _oss_save_json("unread_queue.json", unread_queue)
                    _new_since_save = 0

        except StopIteration:
            print(f"  ❌ 关键词无结果: {query}")
        except Exception as e:
            print(f"  ❌ 处理关键词 '{query}' 出错: {e}")

        # 时间不足时跳过剩余关键词
        remaining = _DEADLINE - time.monotonic()
        if remaining < _MIN_REMAINING:
            print(f"  ⏰ 剩余时间 {remaining:.0f}s，跳过剩余关键词")
            break

    # 最终持久化
    _oss_save_json("unread_queue.json", unread_queue)

    print(f"\n🏁 Step 1 完成: 队列共 {len(unread_queue)} 篇待总结")


# ============================================================
# Step 2: 大模型总结论文（凌晨 2:00）
# ============================================================

def step2_summarize():
    """
    Step 2: 调用大模型 API 总结论文。
    定时触发: 凌晨 2:00

    从 unread_queue 队列取论文，加载独立文件，总结后出队。
    """
    print("=" * 50)
    print("🤖 Step 2: 大模型总结论文")
    print("=" * 50)

    # 加载队列（不再需要 processed_papers.json）
    unread_queue = _oss_load_json("unread_queue.json", [])
    challenge_text = _oss_load_text("challenge_text.txt")

    if challenge_text is None:
        print("❌ 缺少 challenge_text.txt，终止执行")
        return

    if not unread_queue:
        print("❌ 队列为空，终止执行")
        return

    # 每次最多处理 300 篇（从队列尾部，即最近入队的开始）
    MAX_PER_RUN = 300
    start_index = max(0, len(unread_queue) - MAX_PER_RUN)
    to_process = unread_queue[start_index:]
    print(f"📊 队列共 {len(unread_queue)} 篇，本次处理 {len(to_process)} 篇\n")

    # 向后兼容：如果队列中有论文缺少独立文件，一次性加载旧版 extracted_texts.json
    _legacy_texts = None
    _legacy_loaded = False

    # 时间预算
    _DEADLINE = time.monotonic() + 2880
    _MIN_REMAINING = 60

    # 初始化 LLM 客户端
    llm_client = OpenAI(
        api_key=LLM_CONFIG["api_key"],
        base_url=LLM_CONFIG["base_url"],
        timeout=120.0,
    )

    for paper_title in to_process:
        # 时间预算检查
        remaining = _DEADLINE - time.monotonic()
        if remaining < _MIN_REMAINING:
            print(f"  ⏰ 剩余时间不足，停止处理")
            break

        # 按需加载单篇论文文本
        paper_file = f"extracted_texts/{paper_title}.json"
        paper_data = _oss_load_json(paper_file, {})
        paper_text = paper_data.get("text", "") if paper_data else ""

        if not paper_text:
            # 向后兼容：仅在找不到独立文件时，一次性加载旧版 extracted_texts.json
            if not _legacy_loaded:
                _legacy_loaded = True
                print("  📦 检测到旧版 extracted_texts.json，尝试加载（仅一次）...")
                try:
                    _legacy_texts = _oss_load_json("extracted_texts.json", {})
                except Exception as e:
                    print(f"  ⚠️ 旧版文件加载失败: {e}")
                    _legacy_texts = {}
            paper_text = (_legacy_texts or {}).get(paper_title, "")
            if not paper_text:
                print(f"  ⚠️ 未找到文本，跳过: {paper_title}")
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

            # 调用 LLM
            create_kwargs = {
                "model": LLM_CONFIG["model"],
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
            }

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
                # 保存处理结果
                _oss_save_json(f"processed/{paper_title}.json", result)
                # 立即推送 Telegram
                _send_to_telegram(paper_title, result)
                # 立即出队并持久化
                unread_queue.remove(paper_title)
                _oss_save_json("unread_queue.json", unread_queue)
                print(f"✅ 总结完成 + 已推送 TG (队列剩余 {len(unread_queue)}): {paper_title}")
            else:
                print(f"❌ LLM 返回空内容: {paper_title}")

            # API 限速
            remaining = _DEADLINE - time.monotonic()
            if remaining > 120:
                time.sleep(3)
            elif remaining > 60:
                time.sleep(1)

        except Exception as e:
            print(f"❌ 处理论文出错 ({paper_title}): {e}")

    print(f"\n🏁 Step 2 完成: 队列剩余 {len(unread_queue)} 篇")


# ============================================================
# 通道发送器（Step 2 总结完即推 Telegram，飞书可手动触发）
# ============================================================

_HEADERS = [
    "推荐等级", "推荐理由", "任务", "阅读时间", "论文题目",
    "会议/期刊/时间", "科学问题", "挑战", "动机",
    "对现有工作的批判性分析", "贡献", "方法", "数据集",
    "指标", "代码链接", "优势", "核心创新点", "研究目标",
]

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


def _send_telegram_raw(text, parse_mode="Markdown"):
    """发送任意文本到 Telegram，支持自动分段和 Markdown/纯文本回退。"""
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
        prefix = f"📊 周报 ({i+1}/{total})\n\n" if total > 1 else "📊 周报\n\n"
        body = prefix + part

        r = requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": cid, "text": body, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=10,
        ).json()

        if r.get("ok"):
            ok += 1
        elif parse_mode and "parse" in str(r.get("description", "")).lower():
            r2 = requests.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": cid, "text": body, "disable_web_page_preview": True},
                timeout=10,
            ).json()
            if r2.get("ok"):
                ok += 1
            else:
                print(f"  ❌ Telegram 第{i+1}段发送失败: {r2.get('description', '?')}")
        else:
            print(f"  ❌ Telegram 第{i+1}段发送失败: {r.get('description', '?')}")

    print(f"  Telegram: {ok}/{total} 段成功")
    return ok == total


# ============================================================
# 滴答清单 API 完整封装（适配 FC 环境：OSS 替代本地文件，print 替代 loguru）
# ============================================================

_DIDA_DEVICE = json.dumps({
    "platform": "web", "os": "Windows 10",
    "device": "Chrome 86.0.4240.198", "name": "",
    "version": 4130, "id": "6732f9fd4557ba2ce15c00eb",
    "channel": "website", "campaign": "", "websocket": "",
})


# ---- createTask 模板（硬编码替代本地文件） ----
_DIDA_TASK_TEMPLATE = {
    "title": "", "projectId": "", "parentId": "", "columnId": "",
    "tags": [], "priority": 0, "startDate": None, "dueDate": None,
    "id": "", "createdTime": "", "modifiedTime": "", "content": "",
    "kind": "TEXT", "isFloating": False, "reminders": [], "exDate": [],
    "repeatFlag": "", "sortOrder": 0, "progress": 0, "assignee": None,
    "isAllDay": True, "reminderTime": "", "pomodoroSummaries": [],
    "repeateFromTaskId": "", "focusSummaries": [],
}

_DIDA_UPDATE_TASK_TEMPLATE = {
    "add": [], "update": [], "delete": [], "addAttachments": [],
    "updateAttachments": [], "deleteAttachments": [],
}


class DidaAPI:
    """滴答清单 API 端点"""

    def __init__(self):
        self.getProjects = "https://api.dida365.com/api/v2/projects"
        self.createTask = "https://api.dida365.com/api/v2/batch/task"
        self.getHabits = "https://api.dida365.com/api/v2/habits"
        self.getUTCTimetable = "https://api.dida365.com/api/v1/course/timetable"
        self.getColumns = "https://api.dida365.com/api/v2/column?from=0"
        self.getCompleteTasks = "https://api.dida365.com/api/v2/project/{}/completed/?from={}&to={}&limit={}"
        self.createHabit = "https://api.dida365.com/api/v2/habits/batch"
        self.createSubTask = "https://api.dida365.com/api/v2/batch/taskParent"
        self.getColumnsInProject = "https://api.dida365.com/api/v2/column/project/{}"
        self.getAllInfo = "https://api.dida365.com/api/v2/batch/check/0"
        self.login = "https://api.dida365.com/api/v2/user/signon?wc=true&remember=true"


class DidaList:
    """滴答清单操作封装，cookie 缓存到 OSS。"""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; rv2.0.1) Gecko/20100101 Firefox/4.0.1",
            "authority": "api.dida365.com",
            "referer": "https://dida365.com/webapp/",
            "origin": "https://dida365.com",
            "x-device": _DIDA_DEVICE,
        }
        self.__API = DidaAPI()

        # 尝试从 OSS 加载缓存的 cookie
        cached = _oss_load_json("dida_cookie.json", {})
        cookie = cached.get("cookie", "")
        if cookie:
            self.headers["cookie"] = cookie
            self._cookie_loaded = True
        else:
            self._cookie_loaded = False

    def updateCookie(self):
        """登录滴答清单，cookie 缓存到 OSS。"""
        resp = requests.post(
            url=self.__API.login,
            headers=self.headers,
            json={"password": DIDA_CONFIG["password"], "phone": DIDA_CONFIG["phone"]},
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"滴答清单登录失败: {resp.content}")
        print(f"✅ 滴答清单登录成功")

        cookie = ""
        for name, value in resp.cookies.items():
            cookie += f"{name}={value};"

        self.headers["cookie"] = cookie
        _oss_save_json("dida_cookie.json", {"cookie": cookie, "updated": datetime.datetime.now().isoformat()})
        print(f"  ↳ cookie 已缓存到 OSS")

    # ---- 工具方法 ----

    def __getUTCTime(self, t=None):
        """生成滴答清单使用的 UTC 格式化时间字符串。"""
        if t is None:
            t = datetime.datetime.now()
        template = "%Y-%m-%dT%H:%M:%S.000+0000"
        utc_time = t - datetime.timedelta(hours=8)
        return utc_time.strftime(template)

    def __getTimeFromTimeStamp(self, timestamp):
        """根据时间戳生成 UTC 时间字符串。"""
        template = "%Y-%m-%dT%H:%M:%S.000+0000"
        target_time = datetime.datetime.fromtimestamp(timestamp)
        utc_time = target_time - datetime.timedelta(hours=8)
        return utc_time.strftime(template)

    def __generateID(self, num=24):
        """生成滴答清单格式的 ID。"""
        H = "abcdef0123456789" * 3
        return "".join(random.sample(H, num))

    # ---- 核心操作 ----

    def createTask(self, title, projectId="", parentId="", columnId="",
                   tags=None, priority=0, startDate=None, dueDate=None, content=""):
        """创建任务。

        Returns:
            (isSuccess, msg)
        """
        if tags is None:
            tags = []

        task = dict(_DIDA_TASK_TEMPLATE)
        payload = dict(_DIDA_UPDATE_TASK_TEMPLATE)
        payload["add"] = []
        payload["update"] = []
        payload["delete"] = []
        payload["addAttachments"] = []
        payload["updateAttachments"] = []
        payload["deleteAttachments"] = []

        time_str = self.__getUTCTime()

        task["title"] = title
        task["projectId"] = projectId
        task["parentId"] = parentId
        task["columnId"] = columnId
        task["tags"] = tags
        task["priority"] = priority
        task["startDate"] = None if not startDate else self.__getUTCTime(startDate)
        task["dueDate"] = None if not dueDate else self.__getUTCTime(dueDate)
        task["id"] = self.__generateID()
        task["createdTime"] = time_str
        task["modifiedTime"] = time_str
        task["content"] = content

        payload["add"].append(task)

        res = requests.post(
            url=self.__API.createTask,
            headers=self.headers,
            json=payload,
            timeout=15,
        )

        if res.status_code == 200:
            if parentId:
                ok, msg = self._createSubTask(parentId, projectId, task["id"])
                if not ok:
                    return (False, msg)
            return (True, f"create task successfully: {res.content}")
        else:
            return (False, f"create task failed: {res.content}")

    def _createSubTask(self, parentId, projectId, taskId):
        """创建子任务关联。"""
        payload = [{"parentId": parentId, "projectId": projectId, "taskId": taskId}]

        res = requests.post(
            url=self.__API.createSubTask,
            headers=self.headers,
            json=payload,
            timeout=15,
        )

        if res.status_code == 200:
            return (True, f"create subtask successfully: {res.content}")
        else:
            return (False, f"create subtask failed: {res.content}")

    def getFilterTask(self, title=None, projectId=None, parentId=None,
                      columnId=None, tags=None, priority=None,
                      startDate=None, dueDate=None):
        """根据过滤条件返回符合条件的未完成任务。"""

        def _filter(task):
            if title is not None and task.get("title") != title:
                return False
            if projectId is not None and task.get("projectId") != projectId:
                return False
            if parentId is not None and task.get("parentId") != parentId:
                return False
            if columnId is not None and task.get("columnId") != columnId:
                return False
            if tags is not None:
                task_tags = task.get("tags") or []
                if not (set(task_tags) & set(tags)):
                    return False
            if priority is not None and task.get("priority") != priority:
                return False

            if startDate is not None and dueDate is not None:
                if "startDate" in task and "dueDate" in task:
                    ts = datetime.datetime.strptime(task["startDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    ts += datetime.timedelta(hours=8)
                    td = datetime.datetime.strptime(task["dueDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if not (startDate <= ts <= td <= dueDate):
                        return False
                else:
                    return False
            elif startDate is not None:
                if "startDate" in task:
                    td = datetime.datetime.strptime(task["startDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if td.date() != startDate.date():
                        return False
                else:
                    return False
            elif dueDate is not None:
                if "dueDate" in task:
                    td = datetime.datetime.strptime(task["dueDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if td.date() != dueDate.date():
                        return False
                else:
                    return False

            return True

        res = requests.get(url=self.__API.getAllInfo, headers=self.headers, timeout=15)
        all_tasks = res.json().get("syncTaskBean", {}).get("update", [])
        return [t for t in all_tasks if _filter(t)]

    # ---- 查询方法 ----

    def getProjects(self):
        """获取所有清单。"""
        res = requests.get(url=self.__API.getProjects, headers=self.headers, timeout=15)
        return res.json()

    def getColumns(self, projectId=None):
        """获取分组，可指定清单过滤。"""
        res = requests.get(url=self.__API.getColumns, headers=self.headers, timeout=15)
        columns = res.json().get("update", [])

        if projectId is not None:
            return [c for c in columns if c.get("projectId") == projectId]
        return columns

    def getHabits(self):
        """获取所有习惯。"""
        res = requests.get(url=self.__API.getHabits, headers=self.headers, timeout=15)
        return res.json()

    def getCompletedTasks(self, projects=None, startTime=None, endTime=None, limit=200):
        """获取已完成任务。

        Args:
            projects: 清单 ID 列表，None 表示所有
            startTime: 开始时间 (datetime)
            endTime: 结束时间 (datetime)
            limit: 数量限制
        """
        proj_str = ",".join(projects) if projects else "all"

        from_str = ""
        to_str = ""
        if startTime:
            from_str = startTime.strftime("%Y-%m-%d") + "%20" + startTime.strftime("%H:%M:%S")
        if endTime:
            to_str = endTime.strftime("%Y-%m-%d") + "%20" + endTime.strftime("%H:%M:%S")

        url = self.__API.getCompleteTasks.format(proj_str, from_str, to_str, limit)
        res = requests.get(url=url, headers=self.headers, timeout=15)

        tasks = res.json()
        return tasks if isinstance(tasks, list) else []

    def genProjectIDJson(self):
        """将清单 ID 保存到 OSS。"""
        projects = self.getProjects()
        _oss_save_json("dida_projects.json", projects)

    def genColumnIDJson(self):
        """将分组 ID 保存到 OSS。"""
        columns = self.getColumns()
        _oss_save_json("dida_columns.json", columns)

    def genTaskIDJson(self):
        """将未完成任务 ID 保存到 OSS。"""
        tasks = self.getFilterTask()
        _oss_save_json("dida_tasks.json", tasks)


# ============================================================
# Step 4: 每周总结（周一 08:00）
# ============================================================

def step4_weekly_summary():
    """
    Step 4: 每周总结 - 从滴答清单获取本周已完成任务，通过 LLM 生成周报。
    定时触发: 每周一 08:00

    保存到 OSS weekly.json，并发送到 Telegram。
    """
    print("=" * 50)
    print("📊 Step 4: 每周总结（滴答清单）")
    print("=" * 50)

    now = datetime.datetime.now()
    week_ago = now - datetime.timedelta(days=7)
    two_weeks_ago = now - datetime.timedelta(days=14)

    # 1. 获取滴答清单已完成任务（本周 + 上周原始数据用于对比）
    dida = DidaList()
    dida.updateCookie()
    tasks_this_week = dida.getCompletedTasks(startTime=week_ago, endTime=now)

    if not tasks_this_week:
        print("✅ 本周没有已完成任务，跳过生成")
        return

    print(f"📋 本周已完成任务: {len(tasks_this_week)} 条")

    # 上周原始任务数据（用于 LLM 做数据驱动的对比）
    tasks_last_week = dida.getCompletedTasks(startTime=two_weeks_ago, endTime=week_ago)
    print(f"📋 上周已完成任务: {len(tasks_last_week)} 条（用于对比）")

    # 2. 加载历史周报
    weekly = _oss_load_json("weekly.json", {})

    # 3. 初始化 LLM 客户端（复用 OpenRouter 配置）
    llm_client = OpenAI(
        api_key=LLM_CONFIG["api_key"],
        base_url=LLM_CONFIG["base_url"],
        timeout=120.0,
    )

    system_prompt = """
# 角色
你是「周报生成助手」，擅长从任务数据中提炼关键信息，生成结构化中文周报。

# 输入说明
用户会提供滴答清单任务数据，统计范围为自然周（周一至周日），可能包含字段：
任务名称、所属清单/标签、计划耗时、实际耗时、完成状态、执行时间段、关联笔记。
- 若某类字段缺失（如无笔记、无计划耗时），对应板块统一标注「本周无相关数据」，
  不得推测或编造。
- 若未提供上周数据，第 7 板块统一输出：「未提供上周数据，暂无法对比」。

# 统一计算口径（保证每周结果可横向对比）
1. 任务总耗时 = 本周已完成任务的实际耗时之和（单位：小时，保留 1 位小数）
2. 日均耗时 = 总耗时 ÷ 7
3. 完成率 = 已完成任务数 ÷ 本周计划任务总数 × 100%
4. 高频时间段：按任务实际开始时间归入三个时段
   （上午 6:00–12:00 / 下午 12:00–18:00 / 晚上 18:00–24:00），
   取任务数占比最高的时段并标注占比
5. 偏差率 =（实际耗时 − 计划耗时）÷ 计划耗时 × 100%
   （正数为超时，负数为提前，保留整数）
6. 综合效率评分（10 分制）：
   - 完成率得分（0–4 分）= 完成率 × 4
   - 时间把控得分（0–4 分）：平均偏差率 ≤10% 计 4 分；≤25% 计 3 分；
     ≤50% 计 2 分；＞50% 计 1 分
   - 记录质量得分（0–2 分）= 有笔记的任务数 ÷ 已完成任务数 × 2
   - 星级换算：总分 ÷ 2 四舍五入，★ 计 1 星、☆ 计 0 星（共 5 星）

# 输出要求
- Markdown 格式，中文，语言简洁专业
- 第一行固定输出：**⏰ 以下内容由AI自动生成，仅供参考**
- 严格按下方模板输出，板块数量、顺序、条数上限每周保持一致，不自行增减
- 所有数字必须来自输入数据或按上述口径计算，禁止估算和编造
- 待改进点与建议必须具体可执行，禁止空泛表述（如"继续努力"）

# 输出模板（每周固定结构）

## 1. 本周概览
一句话概括本周状态（≤30 字，如「高产但超时明显的一周」）。
● 完成任务 xx/xx 项（完成率 xx%）｜总耗时 xx 小时｜效率评分 x/10

## 2. 时间分析
● 任务总耗时：xx 小时
● 日均耗时：xx 小时
● 高频时间段：xx（任务占比 xx%）

## 3. 效率分析
| 任务类型 | 计划耗时 | 实际耗时 | 偏差率 |
| --- | --- | --- | --- |
（按清单/标签归类，口径每周一致）
综合效率评分：★★★☆☆（x/10）
效率问题诊断（≤2 条：指出超时最严重或完成率最低的类型及可能原因）：
● xx

## 4. 笔记分析
高频关键词（3–5 个）：xx（出现 x 次）
核心结论（≤2 条）：
● xx

## 5. 感想分析
✅ 积极经验（≤2 条）：
● xx
⚠️ 待改进点（≤2 条）：
● xx

## 6. 下周建议（每条一句话、可直接执行）
1. 时间分配：xx
2. 工具优化：xx
3. 习惯调整：xx

## 7. 上周对比
固定对比三个指标（用 ↑/↓/→ 标注变化方向和幅度）：
● 总耗时：xx h → xx h（↑xx%）
● 完成率：xx% → xx%（↓x 个百分点）
● 效率评分：x → x（→）
趋势结论（一句话）：xx

"""

    # 获取上周周报用于对比
    previous_weeks = sorted(weekly.keys(), reverse=True)
    last_week_content = weekly.get(previous_weeks[0], "") if previous_weeks else ""

    user_prompt = f"""
请根据以下滴答清单API返回的任务数据生成本周周报：

【本周任务数据 - 统计范围：{week_ago.strftime('%Y-%m-%d')} 至 {now.strftime('%Y-%m-%d')}】
{json.dumps(tasks_this_week, ensure_ascii=False, indent=2)}

【上周任务数据 - 用于第 7 板块对比，统计范围：{two_weeks_ago.strftime('%Y-%m-%d')} 至 {week_ago.strftime('%Y-%m-%d')}】
{json.dumps(tasks_last_week, ensure_ascii=False, indent=2) if tasks_last_week else "（无上周数据）"}

【上周周报内容 - 用于参考】
{last_week_content or "（无上周周报）"}

"""

    if len(user_prompt) > 100000:
        print("⚠️ 数据过长，截断处理")
        user_prompt = user_prompt[:100000]

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = llm_client.chat.completions.create(
            model=LLM_CONFIG["model"],
            messages=messages,
        )

        if response.choices[0].message.content:
            result = response.choices[0].message.content
            print(f"✅ 周报生成成功 ({len(result)} 字符)")

            # 4. 保存到 OSS
            date_key = now.strftime("%Y-%m-%d")
            weekly[date_key] = result
            _oss_save_json("weekly.json", weekly)

            # 5. 发送到 Telegram
            _send_telegram_raw(result)
        else:
            print("❌ LLM 返回空内容")

    except Exception as e:
        print(f"❌ 周报生成失败: {e}")

    print(f"\n🏁 Step 4 完成")


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
        _send_to_telegram(msg)
        print(f"✅ 已发送 CCF 提醒，共 {len(content_blocks)} 个会议")
    else:
        print("✅ 无 A 类会议即将截止")


# ============================================================
# 统一入口 - 阿里云函数计算 handler
# ============================================================

# 步骤路由表: step → (func, 从 event 提取的额外参数)
_STEP_MAP = {
    "download_extract": (step1_download_and_extract, []),
    "summarize":        (step2_summarize,            []),
    "weekly_summary":   (step4_weekly_summary,       []),
    "ccf_check":        (step_ccf_check,             []),
}


def handler(event, context=None):
    """
    阿里云函数计算统一入口。

    event["step"] 指定步骤:
      "download_extract"  下载论文 + 抽取文本
      "summarize"         大模型总结 + Telegram 推送
      "weekly_summary"    每周总结
      "ccf_check"         CCF 投稿截止提醒
    """
    if isinstance(event, bytes):
        event = event.decode("utf-8")
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            event = {"step": event}

    # FC3 timer 触发器可能将 payload 包装在外层 dict 中（含 triggerTime/triggerName），
    # 此时 event 没有顶层 "step" 键，需从 "payload" 中提取实际的步骤参数。
    if isinstance(event, dict) and "step" not in event and "payload" in event:
        payload = event["payload"]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"step": payload}
        if isinstance(payload, dict):
            event = payload

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
