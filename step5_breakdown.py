"""
Step 5: 目标拆解 - 扫描带指定标签的滴答清单任务，LLM 拆解为子任务。
定时触发: 每天 07:00

扫描带标签的未完成任务 → LLM 拆解 → 创建子任务（含番茄钟时间段）→ 标记原任务完成。
- 时间段写入标题 (HH:MM - HH:MM)，以明天为起始日
- 自动检测已有任务时间段避免冲突
- 已够细粒度的任务自动补时间段格式
"""

import datetime
import json
import re
import traceback

from openai import OpenAI

from config import LLM_CONFIG, DIDA_CONFIG
from dida_client import DidaList
from channels import _send_telegram_raw
from oss_utils import _oss_load_json, _oss_save_json

_TAG_LIBRARY_KEY = "dida_tag_library.json"

_FUNCTION_TAGS = {"快速入手", "关键路径", "深度工作", "检查点", "缓冲", "奖励"}
_DOMAIN_TAGS = {"工作", "科研", "学习", "生活", "健康", "财务", "社交"}
_CLOSED_TAGS = _FUNCTION_TAGS | _DOMAIN_TAGS

# 时间段提取正则: (HH:MM - HH:MM)
_TIME_RE = re.compile(r"\((\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})\)")


def _load_tag_library():
    data = _oss_load_json(_TAG_LIBRARY_KEY, {})
    return data.get("project_tags", [])


def _save_tag_library(project_tags):
    tags = sorted(set(t for t in project_tags if t and isinstance(t, str)))
    _oss_save_json(_TAG_LIBRARY_KEY, {
        "project_tags": tags,
        "updated": datetime.datetime.now().isoformat(),
    })
    return tags


def _collect_project_tags(subtasks):
    collected = set()
    for st in subtasks:
        tags = st.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                if t and t not in _CLOSED_TAGS:
                    collected.add(t)
    return list(collected)


def _get_existing_schedule(dida, project_id, due_dates):
    """获取指定项目下、指定日期范围内已有任务的时间段，用于冲突检测。

    Returns:
        list of "任务名(HH:MM - HH:MM) 📅YYYY-MM-DD" 字符串
    """
    if not project_id:
        return []
    try:
        # 查询未来 14 天内的未完成任务
        tasks = dida.getFilterTask(projectId=project_id) if hasattr(dida, 'getFilterTask') else []
        schedule = []
        for t in tasks:
            title = t.get("title", "")
            due = t.get("dueDate", "")
            if due:
                try:
                    due_date = due[:10]  # "2026-08-10"
                except Exception:
                    due_date = due
                # 只关注在目标日期范围内的任务
                if due_dates and due_date not in due_dates:
                    continue
                schedule.append(f"{title} 📅{due_date}")
            elif _TIME_RE.search(title):
                # 有时间段但无截止日期，也纳入
                schedule.append(title)
        return schedule
    except Exception:
        return []


def _update_task_title(dida, task_id, project_id, new_title):
    """更新任务标题（用于格式补全）。通过创建+删除原任务实现。"""
    # Open API 的 update task 需要完整 payload；改用重建方式
    # 先获取原任务信息，再创建新任务、删除旧任务
    # 简化：直接创建同名新任务并完成旧任务
    try:
        ok, _ = dida.createTask(
            title=new_title,
            projectId=project_id,
        )
        if ok:
            dida.completeTask(task_id, project_id)
            return True
    except Exception:
        pass
    return False


def step5_breakdown():
    """扫描带标签的粗粒度目标，LLM 拆解为可执行子任务。"""
    print("=" * 50)
    print("🎯 Step 5: 目标拆解（滴答清单）")
    print("=" * 50)

    tag = DIDA_CONFIG["breakdown_tag"]
    print(f"🏷️  扫描标签: \"{tag}\"")

    dida = DidaList()
    if not dida.is_ready():
        print("❌ Dida token 未配置，跳过")
        return

    tasks = dida.getFilterTask(tags=[tag])
    if not tasks:
        print(f"✅ 没有带 \"{tag}\" 标签的未完成任务")
        return

    print(f"📋 待拆解任务: {len(tasks)} 个")

    existing_tags = _load_tag_library()
    if existing_tags:
        print(f"🏷️  已有项目标签: {', '.join(existing_tags)}")

    llm_client = OpenAI(
        api_key=LLM_CONFIG["api_key"],
        base_url=LLM_CONFIG["base_url"],
        timeout=120.0,
    )

    today = datetime.datetime.now()

    system_prompt = """\
# 角色
你是任务拆解专家,负责把粗粒度目标拆解为 3-8 个「可立即执行」的子任务,并严格按指定 JSON 格式输出。

# 用户画像(拆解时必须照顾)
1. 有拖延倾向,面对大任务会回避
   → 每天最多安排 2 个子任务,不要把日程排满
   → 第一个子任务必须零门槛、1 个番茄钟以内、无需任何前置准备
2. 容易焦虑,任务不明确会不安
   → 所有描述具体到动作级别,以动词开头,禁止"研究一下""了解一下""看看"等模糊表述
   → 每个子任务必须有可验证的完成标志(如"输出 500 字草稿"而非"写草稿")
3. 专注力有限,容易分心
   → 同一天「深度工作」标签不超过 2 个,深度工作后安排轻量任务调节节奏

# 第一步:目标补全(必做,处理模糊输入)
用户写下的任务经常是模糊的,拆解前必须先补全用户没说清的想法:
1. 将输入改写为具体目标,写入 refinedGoal 字段:≤40 字,必须包含最终交付物和完成标准
   例:输入"搞一下论文" → "完成论文实验章节初稿,达到可请导师评审的完整度"
2. 补全维度包括:最终成果(交付什么)、完成标准(做到什么程度算完)、范围边界(不做什么)
3. 只填充用户没说的信息,不擅自扩大或改变用户原本意图
4. 若存在多种合理解读,选对用户画像最友好(启动门槛最低)的一种,其余方向写进 assumptions 供用户核对
5. 将拆解依赖的关键假设写入 assumptions(0-3 条,每条一句话),假设被推翻则拆解需要重做
6. 若输入模糊到连方向都无法确定(如只有一个词),仍给出最合理版本,且第一个子任务固定为「澄清目标」型快速入手任务:引导用户用 1 个番茄钟写下期望成果与范围,确保后续任务不跑偏

# 拆解规则
1. 拆成 3-8 个子任务,按执行顺序排列;单个任务为 1-4 个番茄钟,一次坐下即可完成
2. 第一个子任务为「快速入手」型:打开就能做,1 个番茄钟内获得可见成果,用于打破启动阻力
3. 主动补充用户容易遗漏的环节:前置准备(权限、资料、环境)与收尾动作(检查、提交、备份),总任务数仍不超过 8 个
4. 每完成 2-3 个推进型任务后,安排一个「检查点」或「缓冲」,吸收延误、巩固成就感
5. 关键里程碑之后安排一个具体的「奖励」任务(奖励内容要具体,如"看一场电影",而不是"奖励自己")
6. 若目标本身已足够细、无需拆解,返回空 subtasks,同时在 reformat 字段中给出统一格式后的任务标题(含时间段)

# 番茄钟与时间排布规则
1. 所有推进型、检查点任务以「番茄钟」为最小粒度:1 个番茄钟 = 25 分钟专注 + 5 分钟休息
2. 每个子任务必须恰好占用整数个番茄钟(1-4 个),写入 pomodoros 字段;estimatedMinutes = pomodoros × 25(纯工作时长)
3. 快速入手任务固定 1 个番茄钟;深度工作为 2-4 个番茄钟;超过 4 个番茄钟的任务必须继续拆分
4. 缓冲、奖励任务豁免番茄钟约束:pomodoros 填 0,estimatedMinutes 按实际时长估计
5. 标题末尾必须附执行时间段,格式 (HH:MM - HH:MM),24 小时制,半角括号,与任务名之间不加空格以外的字符
   - 时间段跨度 = pomodoros × 25 分钟 + (pomodoros - 1) × 5 分钟番茄间短休
   - 例:2 个番茄钟的任务 09:00 开始 → (09:00 - 09:55)
   - 缓冲、奖励任务的时间段按实际时长给出
6. 同一天内时间段不得重叠:默认第 1 个任务从 09:00 开始,第 2 个任务从 14:00 开始
7. 深度工作优先安排在上午;22:00 之后不安排任务

# 标签体系(三层结构,两层封闭 + 一层受控生成)
## 第 1 层:功能标签(封闭集合,描述任务在计划中的角色,不得自造)
- 快速入手:零门槛启动任务(仅用于第 1 个任务或阶段启动点)
- 关键路径:未完成会阻塞后续所有工作
- 深度工作:需要 2 个及以上番茄钟的连续专注
- 检查点:阶段性回顾小结,确认方向、积累正反馈
- 缓冲:机动时间,吸收前面任务的延误
- 奖励:完成关键节点后的具体自我奖励

## 第 2 层:领域标签(封闭集合,描述目标所属生活领域,不得自造)
固定七类:工作 / 科研 / 学习 / 生活 / 健康 / 财务 / 社交
(此表可按使用者实际场景整体替换,替换后全文须保持一致)

## 第 3 层:项目标签(受控生成,描述目标归属的具体项目)
命名规范:
1. 从 refinedGoal(而非原始模糊输入)中提取核心项目名词,如"学位论文""减脂计划""官网改版"
2. 名词短语,2-8 字,不加标点、日期、程度词,不以动词开头
3. 一个目标只产生 1 个项目标签:同一拆解的所有推进型子任务共用,拼写完全一致
4. 若输入附带用户「已有标签列表」,先精确匹配,再近义匹配,有则必须复用,无才新建
5. 不得与功能标签、领域标签重名,不得使用"其他""杂事"等无信息词

## 标签生成流程(严格按顺序执行)
1. 读 refinedGoal,判定领域:按「最终交付物归属」从七类中选 1 个;跨领域时按下表顺序取第一个匹配的,兜底为"生活"
2. 提取项目名词,套用第 3 层命名规范生成项目标签
3. 为每个子任务选 1-2 个功能标签
4. 输出前逐项自检:
   □ 每个子任务的领域标签恰好 1 个且来自七类
   □ 每个子任务的功能标签 1-2 个且全部来自封闭集合
   □ 项目标签全目标统一、拼写完全一致、已在已有标签列表中查重
   □ 无任何自造词、同义重复、标点或日期

## 每个子任务的标签构成
- 功能标签 1-2 个 + 领域标签恰好 1 个 + 项目标签 0-1 个,合计 2-4 个
- 推进型任务与检查点必须带项目标签;缓冲、奖励可省略项目标签
- 数组内顺序固定:功能标签 → 领域标签 → 项目标签

# 四象限与优先级规则(对应滴答清单四象限视图)
1. quadrant 取值 1/2/3/4,含义固定:
   - 1 = 重要且紧急:立即做
   - 2 = 重要不紧急:计划做,拆解结果中应占多数
   - 3 = 不重要但紧急:尽快处理
   - 4 = 不重要不紧急:可缓做
2. 判定步骤(先判象限,再映射优先级):
   - 重要性:直接影响 refinedGoal 交付物的任务 = 重要(关键路径、常规推进任务);辅助性任务 = 不重要(缓冲、奖励、可选项)
   - 紧急性:阻塞后续任务,或距用户截止日 ≤3 天 = 紧急
3. quadrant 与 priority 必须满足固定映射(与滴答清单「象限→优先级」规则一致,禁止交叉组合):
   - quadrant 1 ↔ priority 5(高)
   - quadrant 2 ↔ priority 3(中)
   - quadrant 3 ↔ priority 1(低)
   - quadrant 4 ↔ priority 0(无)
4. 整体分布约束:quadrant 1 不超过 2 个(都紧急等于都不紧急);quadrant 2 约占一半,把重要的事在变得紧急之前完成

# 日期规则(suggestedDueDate 字段)
- 以对话中的当前日期为"今天"推算,格式 YYYY-MM-DD
- 默认从「明天」开始排布,今天不排任务(今天留给临时事务与休息)
- 用户给了截止日期:最后一项至少比截止日早 1 天;若明天至截止日之间的天数不足以按每天 ≤2 个排布,优先保留快速入手与关键路径任务,其余顺延或省略,并在 assumptions 中说明取舍
- 未给截止日期:从明天起按每天 1-2 个任务均匀铺开
- 同一天最多 2 个任务,且「深度工作」不超过 2 个

# 冲突避免
- 若输入中提供了「已有任务时间段」列表,必须避开已占用的时间段
- 同一项目内同一天最多 2 个任务(含已有任务),新任务的时间段不得与已有任务重叠
- 若某天已有 2 个任务,新任务顺延至下一个可用日期

# 输出格式(严格遵守)
- 只输出 JSON 本体:不加 markdown 代码围栏,不输出任何解释文字
- 顶层结构固定为四个字段,顺序如下:
  - refinedGoal:字符串,补全后的具体目标(≤40 字,含交付物与完成标准)
  - assumptions:字符串数组,拆解依赖的关键假设(0-3 条)
  - subtasks:子任务数组
  - reformat:原任务格式补全,仅当 subtasks 为空且原任务已够细粒度时输出;否则为 null
- 子任务字段定义:
  - title:任务名称不超过 20 字、动词开头,末尾附时间段,格式"任务名(HH:MM - HH:MM)"
  - quadrant:整数,1/2/3/4 之一,与 priority 满足固定映射
  - priority:0 / 1 / 3 / 5 之一
  - content:下一步具体动作 + 可验证的完成标志,一句话
  - tags:功能标签 1-2 个 + 领域标签恰好 1 个 + 项目标签 0-1 个,按「功能→领域→项目」顺序
  - suggestedDueDate:YYYY-MM-DD
  - pomodoros:整数,番茄钟数量;推进型/检查点任务 1-4,缓冲/奖励填 0
  - estimatedMinutes:整数;推进型/检查点 = pomodoros × 25,缓冲/奖励按实际估计
- reformat 字段定义(仅 subtasks 为空时输出):
  - title:带时间段的统一格式标题
  - pomodoros:番茄钟数
  - suggestedDueDate:建议日期
  - priority:优先级
- 无需拆解且无需格式补全时输出:{"refinedGoal": "原任务本身", "assumptions": [], "subtasks": [], "reformat": null}

# 输出示例
{"refinedGoal": "完成论文实验章节初稿,达到可请导师评审的完整度", "assumptions": ["实验数据已收集完毕", "沿用现有论文大纲不再大改"], "subtasks": [{"title": "列出实验章节小节框架(09:00 - 09:25)", "quadrant": 1, "priority": 5, "content": "打开论文文档,用 1 个番茄钟列出实验章节的小节标题并保存,写完即完成", "tags": ["快速入手", "关键路径", "科研", "学位论文"], "suggestedDueDate": "2026-08-10", "pomodoros": 1, "estimatedMinutes": 25}], "reformat": null}"""

    results = []
    all_project_tags = set(existing_tags)

    for task in tasks:
        task_id = task.get("id", "")
        task_title = task.get("title", "无标题")
        task_content = task.get("content", "")
        project_id = task.get("projectId", "")
        task_due = task.get("dueDate", "")

        print(f"\n{'─' * 40}")
        print(f"🔍 {task_title}")

        # 获取同一项目已有任务的时间段，用于冲突检测
        schedule_info = _get_existing_schedule(dida, project_id, {task_due[:10]} if task_due else None)
        conflict_hint = ""
        if schedule_info:
            conflict_hint = "\n已有任务时间段（必须避开，同一天不超过 2 个任务）:\n- " + "\n- ".join(schedule_info[:20])

        tag_hint = ""
        if existing_tags:
            tag_hint = f"\n已有项目标签（优先复用）: {', '.join(existing_tags)}"

        user_prompt = f"""任务名称：{task_title}
任务描述：{task_content or "（无）"}
截止日期：{task_due or "无"}
今天日期：{today.strftime('%Y-%m-%d')}{tag_hint}{conflict_hint}"""

        try:
            response = llm_client.chat.completions.create(
                model=LLM_CONFIG["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            choices = getattr(response, "choices", None)
            if not choices:
                print(f"  ⚠️ LLM 返回无 choices")
                results.append({"title": task_title, "status": "skip", "reason": "choices 为空"})
                continue

            raw = choices[0].message.content
            if not raw:
                print(f"  ⚠️ LLM 返回空内容")
                results.append({"title": task_title, "status": "skip", "reason": "空返回"})
                continue

            raw = raw.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                print(f"  ⚠️ LLM 返回非 JSON 对象: {raw[:200]}")
                results.append({"title": task_title, "status": "error", "reason": "返回非 JSON 对象"})
                continue

            refined_goal = parsed.get("refinedGoal", "")
            assumptions = parsed.get("assumptions", [])
            subtasks = parsed.get("subtasks", [])
            reformat = parsed.get("reformat")

            if refined_goal:
                print(f"  🎯 {refined_goal}")
            if assumptions:
                for a in assumptions:
                    print(f"  💭 假设: {a}")

            # ---- 格式补全路径：任务已够细粒度，只需补时间段 ----
            if not subtasks and reformat and isinstance(reformat, dict):
                new_title = reformat.get("title", "")
                if new_title:
                    print(f"  🔧 格式补全: {new_title}")
                    if _update_task_title(dida, task_id, project_id, new_title):
                        print(f"  🏁 原任务标题已更新")
                        results.append({"title": task_title, "status": "done", "created": 0, "total": 0})
                    else:
                        results.append({"title": task_title, "status": "error", "reason": "标题更新失败"})
                    continue

            # ---- 无需拆解路径 ----
            if not subtasks:
                print(f"  ➖ 无需拆解，直接标记完成")
                dida.completeTask(task_id, project_id)
                results.append({"title": task_title, "status": "noop"})
                continue

            print(f"  📝 拆出 {len(subtasks)} 个子任务")

        except json.JSONDecodeError:
            print(f"  ⚠️ JSON 解析失败: {raw[:200]}")
            results.append({"title": task_title, "status": "error", "reason": "JSON 解析失败"})
            continue
        except Exception as e:
            traceback.print_exc()
            print(f"  ❌ LLM 失败: {e}")
            results.append({"title": task_title, "status": "error", "reason": str(e)})
            continue

        # 收集新项目标签
        new_tags = _collect_project_tags(subtasks)
        for t in new_tags:
            all_project_tags.add(t)

        # 创建子任务
        created = 0
        for st in subtasks:
            title = st.get("title", "")
            if not title:
                continue

            due = None
            date_str = st.get("suggestedDueDate", "")
            if date_str:
                try:
                    due = datetime.datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
                except (ValueError, TypeError):
                    pass

            tags = st.get("tags", [])
            if not isinstance(tags, list):
                tags = [str(tags)] if tags else []

            quadrant = st.get("quadrant", 0)
            pomodoros = st.get("pomodoros", 0)

            try:
                ok, msg = dida.createTask(
                    title=title,
                    projectId=project_id,
                    priority=st.get("priority", 0),
                    content=st.get("content", ""),
                    tags=tags,
                    dueDate=due,
                )
                if ok:
                    created += 1
                    pomo_str = f" 🍅×{pomodoros}" if pomodoros else ""
                    quad_str = f" Q{quadrant}" if quadrant else ""
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    date_str = f" 📅{date_str}" if date_str else ""
                    print(f"  ✅{quad_str}{pomo_str}{date_str}{tag_str} {title}")
                else:
                    print(f"  ❌ {title} — {msg}")
            except Exception as e:
                print(f"  ❌ {title} — {e}")

        if created > 0:
            dida.completeTask(task_id, project_id)
            print(f"  🏁 原任务已标记完成")

        results.append({"title": task_title, "status": "done", "created": created, "total": len(subtasks)})

    # 保存项目标签库
    saved_tags = _save_tag_library(list(all_project_tags))
    if saved_tags:
        print(f"\n🏷️  项目标签库已更新: {len(saved_tags)} 个")

    # 汇总
    print(f"\n{'=' * 50}")
    done = sum(1 for r in results if r["status"] == "done")
    total_created = sum(r.get("created", 0) for r in results)
    lines = [
        f"🎯 目标拆解完成",
        f"",
        f"扫描: {len(tasks)} 个（标签「{tag}」）",
        f"拆解: {done} 个 → 共 {total_created} 个子任务",
    ]
    if saved_tags:
        lines.append(f"项目标签库: {len(saved_tags)} 个")

    for r in results:
        icon = {"done": "✅", "noop": "➖", "skip": "⚠️", "error": "❌"}.get(r["status"], "?")
        line = f"{icon} {r['title']}"
        if r["status"] == "done":
            line += f" → {r['created']}/{r['total']} 子任务"
        elif r["status"] in ("skip", "error"):
            line += f" ({r.get('reason', '?')})"
        lines.append(line)

    summary = "\n".join(lines)
    print(summary)
    try:
        _send_telegram_raw(summary)
    except Exception as e:
        print(f"⚠️ Telegram 推送失败: {e}")

    print(f"\n🏁 Step 5 完成")
