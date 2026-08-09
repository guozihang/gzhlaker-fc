"""
Step 2: 调用大模型 API 总结论文。
定时触发: 02:00 / 10:00 / 18:00

从 unread_queue 队列取论文，加载独立文件，总结后出队。
"""

import json
import time

from openai import OpenAI

from config import LLM_CONFIG
from oss_utils import _oss_load_json, _oss_save_json, _oss_load_text
from channels import _send_to_telegram


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


def step2_summarize():
    """
    Step 2: 调用大模型 API 总结论文。

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
