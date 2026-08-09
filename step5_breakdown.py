"""
Step 5: 目标拆解 - 扫描带指定标签的滴答清单任务，LLM 拆解为子任务。
定时触发: 每天 07:00

扫描带标签的未完成任务 → LLM 拆解 → 创建子任务（含四象限）→ 标记原任务完成。
标签库自动维护: 项目标签存入 OSS，LLM 优先复用。
"""

import datetime
import json
import traceback

from openai import OpenAI

from config import LLM_CONFIG, DIDA_CONFIG
from dida_client import DidaList
from channels import _send_telegram_raw
from oss_utils import _oss_load_json, _oss_save_json

_TAG_LIBRARY_KEY = "dida_tag_library.json"

# 功能标签（封闭集合）
_FUNCTION_TAGS = {"快速入手", "关键路径", "深度工作", "检查点", "缓冲", "奖励"}

# 领域标签（封闭七类）
_DOMAIN_TAGS = {"工作", "科研", "学习", "生活", "健康", "财务", "社交"}

# 所有封闭标签（不是项目标签的）
_CLOSED_TAGS = _FUNCTION_TAGS | _DOMAIN_TAGS


def _load_tag_library():
    """从 OSS 加载已有项目标签列表。"""
    data = _oss_load_json(_TAG_LIBRARY_KEY, {})
    return data.get("project_tags", [])


def _save_tag_library(project_tags):
    """将项目标签列表写入 OSS。"""
    tags = sorted(set(t for t in project_tags if t and isinstance(t, str)))
    _oss_save_json(_TAG_LIBRARY_KEY, {
        "project_tags": tags,
        "updated": datetime.datetime.now().isoformat(),
    })
    return tags


def _collect_project_tags(subtasks):
    """从子任务中提取项目标签（排除功能标签和领域标签后剩下的）。"""
    collected = set()
    for st in subtasks:
        tags = st.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                if t and t not in _CLOSED_TAGS:
                    collected.add(t)
    return list(collected)


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
   → 第一个子任务必须零门槛、≤30 分钟、无需任何前置准备
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
6. 若输入模糊到连方向都无法确定(如只有一个词),仍给出最合理版本,且第一个子任务固定为「澄清目标」型快速入手任务:引导用户用 ≤15 分钟写下期望成果与范围,确保后续任务不跑偏

# 拆解规则
1. 拆成 3-8 个子任务,按执行顺序排列;单个任务粒度控制在 30-120 分钟,一次坐下即可完成
2. 第一个子任务为「快速入手」型:打开就能做,30 分钟内获得可见成果,用于打破启动阻力
3. 主动补充用户容易遗漏的环节:前置准备(权限、资料、环境)与收尾动作(检查、提交、备份),总任务数仍不超过 8 个
4. 每完成 2-3 个推进型任务后,安排一个「检查点」或「缓冲」,吸收延误、巩固成就感
5. 关键里程碑之后安排一个具体的「奖励」任务(奖励内容要具体,如"看一场电影",而不是"奖励自己")
6. 若目标本身已足够细、无需拆解,返回空 subtasks(见输出格式)

# 标签体系(三层结构,两层封闭 + 一层受控生成)
## 第 1 层:功能标签(封闭集合,描述任务在计划中的角色,不得自造)
- 快速入手:零门槛启动任务(仅用于第 1 个任务或阶段启动点)
- 关键路径:未完成会阻塞后续所有工作
- 深度工作:需要 ≥60 分钟连续专注
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
- 用户给了截止日期:任务从今天起排布,最后一项至少比截止日早 1 天,预留收尾余量
- 未给截止日期:从今天起按每天 1-2 个任务均匀铺开
- 同一天最多 2 个任务,且「深度工作」不超过 2 个

# 输出格式(严格遵守)
- 只输出 JSON 本体:不加 markdown 代码围栏,不输出任何解释文字
- 顶层结构固定为三个字段,顺序如下:
  - refinedGoal:字符串,补全后的具体目标(≤40 字,含交付物与完成标准)
  - assumptions:字符串数组,拆解依赖的关键假设(0-3 条)
  - subtasks:子任务数组
- 子任务字段定义:
  - title:不超过 20 字,动词开头
  - quadrant:整数,1/2/3/4 之一,与 priority 满足固定映射
  - priority:0 / 1 / 3 / 5 之一
  - content:下一步具体动作 + 可验证的完成标志,一句话
  - tags:功能标签 1-2 个 + 领域标签恰好 1 个 + 项目标签 0-1 个,按「功能→领域→项目」顺序
  - suggestedDueDate:YYYY-MM-DD
  - estimatedMinutes:整数,预估耗时(分钟)
- 无需拆解时输出:{"refinedGoal": "原任务本身", "assumptions": [], "subtasks": []}

# 输出示例
{"refinedGoal": "完成论文实验章节初稿,达到可请导师评审的完整度", "assumptions": ["实验数据已收集完毕", "沿用现有论文大纲不再大改"], "subtasks": [{"title": "列出实验章节小节框架", "quadrant": 1, "priority": 5, "content": "打开论文文档,花 20 分钟列出实验章节的小节标题并保存,写完即完成", "tags": ["快速入手", "关键路径", "科研", "学位论文"], "suggestedDueDate": "2026-08-10", "estimatedMinutes": 25}]}"""

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

        tag_hint = ""
        if existing_tags:
            tag_hint = f"\n已有项目标签（优先复用）: {', '.join(existing_tags)}"

        user_prompt = f"""任务名称：{task_title}
任务描述：{task_content or "（无）"}
截止日期：{task_due or "无"}
今天日期：{today.strftime('%Y-%m-%d')}{tag_hint}"""

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

            if refined_goal:
                print(f"  🎯 {refined_goal}")
            if assumptions:
                for a in assumptions:
                    print(f"  💭 假设: {a}")

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

            # quadrant 用于日志展示
            quadrant = st.get("quadrant", 0)

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
                    quad_str = f" Q{quadrant}" if quadrant else ""
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    date_str = f" 📅{date_str}" if date_str else ""
                    print(f"  ✅{quad_str}{date_str}{tag_str} {title}")
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
