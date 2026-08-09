"""
Step 4: 每周总结 - 从滴答清单获取本周已完成任务，通过 LLM 生成周报。
定时触发: 每周一 08:00

保存到 OSS weekly.json，并发送到 Telegram。
"""

import datetime
import json

from openai import OpenAI

from config import LLM_CONFIG
from oss_utils import _oss_load_json, _oss_save_json
from dida_client import DidaList
from channels import _send_telegram_raw


def step4_weekly_summary():
    """
    Step 4: 每周总结 - 从滴答清单获取本周已完成任务，通过 LLM 生成周报。

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
