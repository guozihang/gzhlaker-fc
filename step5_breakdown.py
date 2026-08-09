"""
Step 5: 目标拆解 - 扫描带指定标签的滴答清单任务，LLM 拆解为子任务。
定时触发: 每天 07:00

扫描带标签的未完成任务 → LLM 拆解 → 创建子任务 → 标记原任务完成。
标签库自动维护: 从 OSS 加载已有标签 → 传给 LLM 复用 → 新标签写回 OSS。
"""

import datetime
import json

from openai import OpenAI

from config import LLM_CONFIG, DIDA_CONFIG
from dida_client import DidaList
from channels import _send_telegram_raw
from oss_utils import _oss_load_json, _oss_save_json

# 标签库存放路径
_TAG_LIBRARY_KEY = "dida_tag_library.json"


def _load_tag_library():
    """从 OSS 加载已有标签库，返回领域标签列表。"""
    data = _oss_load_json(_TAG_LIBRARY_KEY, {})
    return data.get("domain_tags", [])


def _save_tag_library(domain_tags):
    """将领域标签列表写入 OSS。"""
    # 去重排序
    tags = sorted(set(tag for tag in domain_tags if tag and isinstance(tag, str)))
    _oss_save_json(_TAG_LIBRARY_KEY, {
        "domain_tags": tags,
        "updated": datetime.datetime.now().isoformat(),
    })
    return tags


def _collect_domain_tags(subtasks):
    """从 LLM 返回的子任务中提取所有标签，只保留非功能标签的（领域标签）。

    功能标签白名单: 快速入手, 关键路径, 深度工作, 检查点, 缓冲, 奖励
    不在白名单内的视为领域标签。
    """
    FUNCTION_TAGS = {"快速入手", "关键路径", "深度工作", "检查点", "缓冲", "奖励"}
    collected = set()
    for st in subtasks:
        tags = st.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                if t and t not in FUNCTION_TAGS:
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

    # 加载已有标签库
    existing_tags = _load_tag_library()
    if existing_tags:
        print(f"🏷️  已有领域标签: {', '.join(existing_tags)}")

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

# 拆解规则
1. 拆成 3-8 个子任务,按执行顺序排列;单个任务粒度控制在 30-120 分钟,一次坐下即可完成
2. 第一个子任务为「快速入手」型:打开就能做,30 分钟内获得可见成果,用于打破启动阻力
3. 每完成 2-3 个推进型任务后,安排一个「检查点」或「缓冲」,吸收延误、巩固成就感
4. 关键里程碑之后安排一个具体的「奖励」任务(奖励内容要具体,如"看一场电影",而不是"奖励自己")
5. 若目标本身已足够细、无需拆解,返回空数组(见输出格式)
6. 若目标描述模糊,按最合理的理解拆解,不要反问,确保第一个任务仍可直接执行

# 标签体系(双层结构)
## A. 功能标签(封闭集合,描述任务在计划中的角色,不得自造)
- 快速入手:零门槛启动任务(仅用于第 1 个任务或阶段启动点)
- 关键路径:未完成会阻塞后续所有工作
- 深度工作:需要 ≥60 分钟连续专注
- 检查点:阶段性回顾小结,确认方向、积累正反馈
- 缓冲:机动时间,吸收前面任务的延误
- 奖励:完成关键节点后的具体自我奖励

## B. 领域标签(自动构建,描述任务所属主题/项目)
命名与构建规范:
1. 从目标的主题、项目或领域中提炼,如"论文写作""实验""文献阅读""健身"
2. 名词短语,2-6 字,粒度适中:能覆盖该目标下多个子任务,不要太宽(如"学习")也不要太窄(如"论文第三章修改")
3. 同一目标内含义统一:同一概念全程只用同一个标签,禁止同义词混用(如"论文"与"写论文"并存)
4. 每个目标的领域标签总数 ≤ 5 个,避免标签爆炸
5. 不得与功能标签重名,不得使用"其他""杂事"等无信息词
6. 若输入中附带用户「已有标签列表」,优先从中复用,无匹配时再按以上规范新建

## 每个子任务的标签构成
- 功能标签 1-2 个 + 领域标签 0-2 个,合计 2-4 个
- 推进型任务必须至少 1 个领域标签;缓冲、奖励任务可省略领域标签
- 数组内顺序固定:功能标签在前,领域标签在后

# 优先级规则(priority 字段)
- 5 = 高:关键路径任务
- 3 = 中:常规推进任务
- 1 = 低:缓冲、奖励任务
- 0 = 普通:可选/锦上添花任务

# 日期规则(suggestedDueDate 字段)
- 以对话中的当前日期为"今天"推算,格式 YYYY-MM-DD
- 用户给了截止日期:任务从今天起排布,最后一项至少比截止日早 1 天,预留收尾余量
- 未给截止日期:从今天起按每天 1-2 个任务均匀铺开
- 同一天最多 2 个任务,且「深度工作」不超过 2 个

# 输出格式(严格遵守)
- 只输出 JSON 本体:不加 markdown 代码围栏,不输出任何解释文字
- 字段定义:
  - title:不超过 20 字,动词开头
  - priority:0 / 1 / 3 / 5 之一
  - content:下一步具体动作 + 可验证的完成标志,一句话
  - tags:功能标签 1-2 个 + 领域标签 0-2 个,功能标签在前
  - suggestedDueDate:YYYY-MM-DD
  - estimatedMinutes:整数,预估耗时(分钟)
- 无需拆解时输出:{"subtasks": []}

# 输出示例
{"subtasks": [{"title": "列出论文三级大纲", "priority": 5, "content": "打开论文文档,花 20 分钟列出到章节级的三级大纲并保存,写完即完成", "tags": ["快速入手", "关键路径", "论文写作"], "suggestedDueDate": "2026-08-10", "estimatedMinutes": 25}]}"""

    results = []
    all_new_tags = set(existing_tags)

    for task in tasks:
        task_id = task.get("id", "")
        task_title = task.get("title", "无标题")
        task_content = task.get("content", "")
        project_id = task.get("projectId", "")
        task_due = task.get("dueDate", "")

        print(f"\n{'─' * 40}")
        print(f"🔍 {task_title}")

        # 构建 user prompt，附带已有标签库
        tag_hint = ""
        if existing_tags:
            tag_hint = f"\n已有领域标签（优先复用）: {', '.join(existing_tags)}"

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
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content
            if not raw:
                print(f"  ⚠️ LLM 返回空")
                results.append({"title": task_title, "status": "skip", "reason": "空返回"})
                continue

            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                print(f"  ⚠️ LLM 返回非 JSON 对象: {raw[:200]}")
                results.append({"title": task_title, "status": "error", "reason": "返回非 JSON 对象"})
                continue

            subtasks = parsed.get("subtasks", [])
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
            print(f"  ❌ LLM 失败: {e}")
            results.append({"title": task_title, "status": "error", "reason": str(e)})
            continue

        # 收集新领域标签
        new_tags = _collect_domain_tags(subtasks)
        for t in new_tags:
            all_new_tags.add(t)

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
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    date_str = f" 📅{date_str}" if date_str else ""
                    print(f"  ✅{date_str}{tag_str} {title}")
                else:
                    print(f"  ❌ {title} — {msg}")
            except Exception as e:
                print(f"  ❌ {title} — {e}")

        if created > 0:
            dida.completeTask(task_id, project_id)
            print(f"  🏁 原任务已标记完成")

        results.append({"title": task_title, "status": "done", "created": created, "total": len(subtasks)})

    # 保存标签库
    saved_tags = _save_tag_library(list(all_new_tags))
    if saved_tags:
        print(f"\n🏷️  标签库已更新: {len(saved_tags)} 个领域标签")

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
        lines.append(f"标签库: {len(saved_tags)} 个领域标签")

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
