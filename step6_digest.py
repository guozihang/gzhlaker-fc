"""
Step 6: 每日信息速递 — GitHub Trending / 油价 / 天气 / 有用新闻。
定时触发: 每天 07:00

四个板块各自独立容错，任一抓取失败只省略该板块。
Telegram 先发一条 TTS 语音（尽力而为，失败退化为纯文字），再发文字。
去重账本存 OSS news_seen.json，保留 NEWS_RETENTION_DAYS 天。
"""

import datetime
import email.utils
import hashlib
import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from openai import OpenAI

from config import LLM_CONFIG, NEWS_CONFIG
from oss_utils import _oss_load_json, _oss_save_json
from channels import _send_telegram_raw, _send_telegram_audio


_SOURCES = [
    {"name": "Hacker News Best", "url": "https://hnrss.org/best"},
    {"name": "量子位", "url": "https://www.qbitai.com/feed"},
]

_TRENDING_URL = "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml"

_WEATHER_URL_DEFAULT = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=40.842&longitude=111.749"
    "&hourly=temperature_2m,precipitation_probability,wind_speed_10m,weathercode"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
    "&timezone=Asia/Shanghai&forecast_days=1"
)

_ATOM_Q = "{http://www.w3.org/2005/Atom}"

# WMO weather code → 中文天气词
_WMO_TEXT = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "小冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


# ============================================================
# 通用工具
# ============================================================

def _digest_hash(url, feed_name, title):
    """URL 规范化后 sha1 前 16 位；无 URL 时用 feed|title。"""
    key = (url or "").strip()
    if key:
        parts = urllib.parse.urlsplit(key)
        key = "%s://%s%s" % (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"))
    else:
        key = "%s|%s" % (feed_name, title)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _fetch(url, timeout=30):
    """GET 请求。https 失败时回退 http（油价站只有 http 通）。"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    candidates = [url] if url.startswith("http://") else [url, url.replace("https://", "http://", 1)]
    last = None
    for u in candidates:
        try:
            resp = requests.get(u, timeout=timeout, headers=headers)
            if resp.status_code == 200 and resp.content:
                return resp.content
            last = "HTTP %s" % resp.status_code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
    raise RuntimeError(last or "fetch failed")


def _clean_html(raw, limit=400):
    """去标签 + unescape + 压缩空白 + 截断。"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_date(text):
    """RFC-822 / ISO-8601 → aware datetime；失败返回 None，绝不抛异常。

    FC 运行时是 Python 3.10：fromisoformat 不认 'Z' 后缀（3.11 才支持），
    parsedate_to_datetime 对无时区串返回 naive datetime —— 都必须防御。
    """
    if not text:
        return None
    s = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(s).astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _parse_rss(raw, feed_name):
    """RSS 2.0 / Atom 通用解析 → [{feed,title,link,date,summary}]，绝不抛异常。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ⚠️ {feed_name} XML 解析失败: {e}")
        return []

    items, is_atom = [], root.tag == _ATOM_Q + "feed"
    nodes = root.findall(_ATOM_Q + "entry") if is_atom else list(root.iter("item"))
    for node in nodes:
        if is_atom:
            title = _clean_html(node.findtext(_ATOM_Q + "title") or "", 200)
            link = ""
            for l in node.findall(_ATOM_Q + "link"):
                rel = l.get("rel")
                if rel in (None, "alternate") or not link:
                    link = l.get("href") or ""
            date = _parse_date(node.findtext(_ATOM_Q + "published")
                               or node.findtext(_ATOM_Q + "updated"))
            summary = _clean_html(node.findtext(_ATOM_Q + "content")
                                  or node.findtext(_ATOM_Q + "summary") or "")
        else:
            title = _clean_html(node.findtext("title") or "", 200)
            link = (node.findtext("link") or "").strip()
            date = _parse_date(node.findtext("pubDate")
                               or node.findtext("{http://purl.org/dc/elements/1.1/}date"))
            summary = _clean_html(node.findtext("description") or "")
        if not title:
            continue
        items.append({"feed": feed_name, "title": title, "link": link,
                      "date": date, "summary": summary})
    return items


def _llm_complete(system_prompt, user_prompt, json_mode=False, temperature=0.3):
    """调用 LLM，最多 2 次重试，识别 OpenRouter 安全过滤响应。

    返回正文字符串；全部失败返回 None（不抛异常）。
    """
    client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"], timeout=120.0)
    kwargs = {
        "model": LLM_CONFIG["model"],
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(2):
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as e:
            print(f"  ⚠️ LLM 调用异常 (attempt {attempt+1}): {type(e).__name__}: {e}")
            time.sleep(1)
            continue

        choices = getattr(response, "choices", None)
        if not choices:
            time.sleep(1)
            continue

        raw = (choices[0].message.content or "").strip()
        if not raw:
            time.sleep(1)
            continue

        # OpenRouter 安全过滤会返回 "User Safety: safe" 之类的非正文内容
        if raw.lower().startswith("user safety") or raw.lower().startswith("safety"):
            print(f"  ⚠️ 安全过滤拦截 (attempt {attempt+1}): {raw[:100]}")
            time.sleep(1)
            continue

        return raw

    print("  ⚠️ LLM 两次尝试均失败")
    return None


# ============================================================
# 板块 1: GitHub Trending
# ============================================================

def _trending_items(seen):
    """解析 GitHub Trending RSS，返回 [(repo名, 简介, 链接, 排名变化)]。"""
    try:
        raw = _fetch(_TRENDING_URL)
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  ⚠️ GitHub Trending 抓取/解析失败: {type(e).__name__}: {e}")
        return []
    result = []
    for node in list(root.iter("item"))[:NEWS_CONFIG["trending_n"]]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title:
            continue
        desc = node.findtext("description") or ""
        # description 是巨型 HTML：第一个 <p> 是 repo 简介，<hr> 之后是 README
        m = re.search(r"<p>(.*?)</p>", desc, re.S)
        intro = _clean_html(m.group(1) if m else "", 100) or "—"
        h = _digest_hash(link, "trending", title)
        prev = seen.get(h, {})
        if prev:
            move = "(排名变化)" if prev.get("stamp") != title else ""
        else:
            move = "(新上榜)"
        seen[h] = {"date": datetime.date.today().isoformat(), "section": "trending", "stamp": title}
        result.append((title, intro, link, move))
    return result


# ============================================================
# 板块 2: 油价（内蒙古，全国统一调价）
# ============================================================

def _oil_items(seen):
    """抓取油价页面：当前价 + 下次调价涨跌预测 + 与上次记录对比。"""
    try:
        raw = _fetch(NEWS_CONFIG["oil_page"], timeout=20)
    except Exception as e:
        print(f"  ⚠️ 油价抓取失败: {e}")
        return None
    page = raw.decode("utf-8", errors="replace")

    prices = {}
    for label in ("92#", "95#", "98#", "0#柴油"):
        m = re.search(r"内蒙古%s[^<]*</dt>\s*<dd>([\d.]+)</dd>" % re.escape(label), page)
        if m:
            prices[label] = m.group(1)
    if not prices:
        print("  ⚠️ 油价页面解析失败（可能改版）")
        return None

    adjust, rmb_t, yl_range = "", "", ""
    m = re.search(r"下次油价(\d+)月(\d+)日24时调整", page)
    if m:
        adjust = f"{m.group(1)}月{m.group(2)}日24时"
    m = re.search(r"预计(上调|下调|搁浅)([\d.]+)元/吨(?:\(([^)]+)\))?", page)
    if m:
        direction = {"上调": "↑ 预计上调", "下调": "↓ 预计下调", "搁浅": "预计搁浅"}[m.group(1)]
        rmb_t = f"{direction} {m.group(2)}元/吨"
        yl_range = m.group(3) or ""

    # 与账本中上次记录对比（每次运行都记录 92# 值）
    key = "oil:neimenggu"
    prev = seen.get(key, {})
    delta = ""
    if "oil92" in prev:
        try:
            d = round(float(prices["92#"]) - float(prev["oil92"]), 2)
            if abs(d) >= 0.005:
                delta = "92# %s%.2f 元/升" % ("+" if d > 0 else "", d)
            else:
                delta = "92# 与上次持平"
        except (ValueError, KeyError):
            delta = ""
    seen[key] = {"date": datetime.date.today().isoformat(), "section": "oil",
                 "oil92": prices.get("92#", "")}

    return {"prices": prices, "adjust": adjust, "rmb_t": rmb_t,
            "yl_range": yl_range, "delta": delta}


# ============================================================
# 板块 3: 天气（呼和浩特）
# ============================================================

def _weather_items():
    """Open-Meteo 免密钥天气 → dict；失败返回 None。"""
    url = NEWS_CONFIG["weather_url"] or _WEATHER_URL_DEFAULT
    try:
        data = requests.get(url, timeout=20).json()
    except Exception as e:
        print(f"  ⚠️ 天气抓取失败: {e}")
        return None
    try:
        daily = data["daily"]
        hourly = data["hourly"]
        return {
            "t_max": round(daily["temperature_2m_max"][0]),
            "t_min": round(daily["temperature_2m_min"][0]),
            "weather": _WMO_TEXT.get(daily["weathercode"][0], "—"),
            "precip": max(hourly["precipitation_probability"] or [0]),
            "wind": max(hourly["wind_speed_10m"] or [0]),
        }
    except (KeyError, IndexError, TypeError) as e:
        print(f"  ⚠️ 天气数据解析失败: {e}")
        return None


# ============================================================
# 板块 4: 有用新闻（一次 LLM 批量筛选）
# ============================================================

_NEWS_FILTER_SYSTEM = """你是一位资深科技编辑，从候选新闻中挑选真正有价值的内容。硬排除：明星花边/娱乐八卦、营销软文、纯广告、标题党、低信息量转载、AI 批量生成的垃圾文。入选标准：信息密度高、有具体可引用事实（技术进展、研究成果、工具资源、重大行业事件、政策法规）。输出严格 JSON，格式：{"items":[{"id":整数,"include":true,"summary":"中文一句话摘要","importance":1到10的整数}]}。对输入中的每个 id 必须给出一条决策，不可漏报、不可虚构 id。全部判为 include:false 也是合法输出。入选条目按 importance 从高到低排序。"""


def _news_fetch_and_filter(sources, seen):
    """抓取所有源（48h 窗口内），跨源轮转凑候选，一次 LLM 批量筛选。

    返回 (selected, seen_accumulated)。seen_accumulated 是 seen 的浅拷贝副本，
    对原 seen 无副作用——LLM 失败时主流程直接丢弃副本，候选不被烧掉。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    window = datetime.timedelta(hours=48)
    per_feed = []
    seen_acc = dict(seen)

    for src in sources:
        try:
            raw = _fetch(src["url"])
        except Exception as e:
            print(f"  ⚠️ 新闻源 {src['name']} 抓取失败: {e}")
            continue
        items = _parse_rss(raw, src["name"])
        recent = []
        for it in items:
            if it["date"] is None or now - it["date"] <= window:
                h = _digest_hash(it["link"], src["name"], it["title"])
                if h in seen_acc:
                    continue
                seen_acc[h] = {"date": datetime.date.today().isoformat(),
                               "section": "news", "stamp": it["title"]}
                recent.append(it)
        recent.sort(key=lambda x: x["date"] or datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
                    reverse=True)
        per_feed.append(recent[:10])
        print(f"  📥 {src['name']}: {len(recent)} 条新候选")

    # 跨源轮转，防止单一高产源挤掉其他源
    candidates = []
    idx = 0
    max_len = max((len(f) for f in per_feed), default=0)
    while len(candidates) < 40 and idx < max_len:
        for feed_items in per_feed:
            if idx < len(feed_items) and len(candidates) < 40:
                candidates.append(feed_items[idx])
        idx += 1
    if not candidates:
        return [], seen_acc

    lines = []
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] ({c['feed']}) {c['title']}\nURL: {c['link']}\n摘要: {c['summary']}")
    user_prompt = ("以下是今天抓取的候选新闻，逐条判断是否有营养（硬排除花边/软文/标题党），"
                   f"最多入选 {NEWS_CONFIG['news_max']} 条：\n\n" + "\n\n".join(lines))

    raw = _llm_complete(_NEWS_FILTER_SYSTEM, user_prompt, json_mode=True, temperature=0.3)
    if not raw:
        raise RuntimeError("新闻筛选 LLM 无有效返回")

    data = json.loads(raw)
    selected = []
    for item in data.get("items", []):
        if not item.get("include"):
            continue
        try:
            c = candidates[int(item["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        selected.append({"feed": c["feed"], "title": c["title"], "link": c["link"],
                         "summary": str(item.get("summary") or "")[:120],
                         "importance": int(item.get("importance") or 5)})
    selected.sort(key=lambda x: -x["importance"])
    return selected[:NEWS_CONFIG["news_max"]], seen_acc


# ============================================================
# 语音稿 + TTS
# ============================================================

_SCRIPT_SYSTEM = """你是每日速递的播报员。根据给定的信息写一段口语化的晨间速递稿，用简体中文，字数不超过 400 字，适合朗读。要求：1) 开头简短问候+日期；2) 天气后必须给出今日穿衣推荐（结合气温、风力、降水）；3) 油价说明涨跌方向；4) GitHub 和新闻各挑 1-2 条最值得说的简述；5) 不说"以上是"之类的书面套话，语气自然。只输出速递稿正文，不要任何其他文字。"""


def _build_script(sections):
    """用 LLM 生成语音稿（含穿衣推荐）；失败时用简单模板拼稿。"""
    info = "\n\n".join(f"[{k}]\n{v}" for k, v in sections.items() if v)
    if not info:
        return ""

    script = _llm_complete(_SCRIPT_SYSTEM, info, temperature=0.7)
    if script:
        return script[:600]

    # 模板兜底（去板块标题行和尾部标点，口语化拼接）
    print("  ⚠️ 语音稿生成失败，使用模板")
    date_str = datetime.date.today().strftime("%m月%d日")
    parts = [f"早上好，今天是{date_str}"]
    if sections.get("weather"):
        lines = [l for l in sections["weather"].split("\n") if l and not l.startswith("🌤")]
        parts.append("\n".join(lines))
    if sections.get("oil"):
        lines = [l for l in sections["oil"].split("\n") if l and not l.startswith("⛽")]
        parts.append("油价方面，" + " ".join(lines))
    if sections.get("trending"):
        lines = sections["trending"].split("\n")
        parts.append("GitHub 趋势榜，" + lines[1].strip() if len(lines) > 1 else "GitHub 趋势榜")
    if sections.get("news"):
        lines = sections["news"].split("\n")
        parts.append("今天值得关注的新闻，" + lines[1].strip() if len(lines) > 1 else "今天值得关注的新闻")
    clean = [p.rstrip("。") for p in parts if p]
    return "。".join(clean) + "。"


def _tts_to_mp3(script):
    """OpenRouter /audio/speech → /tmp/daily_digest.mp3，返回路径；失败返回 None。"""
    if not script:
        return None
    try:
        client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"], timeout=120.0)
        kwargs = {"model": NEWS_CONFIG["tts_model"], "input": script, "response_format": "mp3"}
        if NEWS_CONFIG["tts_voice"]:
            kwargs["voice"] = NEWS_CONFIG["tts_voice"]
        resp = client.audio.speech.create(**kwargs)
        audio = resp.read() if hasattr(resp, "read") else resp
        path = "/tmp/daily_digest.mp3"
        with open(path, "wb") as f:
            f.write(audio)
        print(f"✅ TTS 生成成功 ({len(audio)} 字节, 模型 {NEWS_CONFIG['tts_model']})")
        return path
    except Exception as e:
        print(f"  ⚠️ TTS 失败（退化为纯文字）: {type(e).__name__}: {e}")
        return None


# ============================================================
# 去重账本
# ============================================================

def _prune_and_save(seen):
    """去重账本剪枝（只留 retention_days 内）+ 落盘。"""
    today = datetime.date.today()
    keep = datetime.timedelta(days=NEWS_CONFIG["news_retention_days"])
    pruned = {}
    for k, v in (seen or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            d = datetime.date.fromisoformat(str(v.get("date", "")))
        except ValueError:
            continue
        if today - d <= keep:
            pruned[k] = v
    _oss_save_json("news_seen.json", pruned)


# ============================================================
# 入口
# ============================================================

def step6_daily_digest():
    """
    Step 6: 每日信息速递。定时触发: 每天 07:00。

    四板块: GitHub Trending / 油价（内蒙古）/ 呼和浩特天气 / 有用新闻。
    Telegram 先发语音（尽力而为，失败退化纯文字），再发文字。
    """
    print("=" * 50)
    print("📰 Step 6: 每日信息速递")
    print("=" * 50)

    _DEADLINE = time.monotonic() + 2880
    _MIN_REMAINING = 60

    seen = _oss_load_json("news_seen.json", {})

    # ---- 抓取四板块（各板块独立容错）----
    trending = _trending_items(seen)
    oil = _oil_items(seen)
    weather = _weather_items()
    try:
        news, seen_acc = _news_fetch_and_filter(_SOURCES, seen)
        news_ok = True
    except Exception as e:
        print(f"  ⚠️ 新闻筛选失败，本板块省略: {type(e).__name__}: {e}")
        news, seen_acc, news_ok = [], None, False

    # ---- 组装文字 ----
    sections = {}
    if trending:
        lines = []
        for i, (title, intro, link, move) in enumerate(trending, 1):
            lines.append("%d. %s — %s" % (i, title, intro))
            if link:
                lines.append("   %s" % link)
            if move:
                lines[-1] += "  %s" % move
        sections["trending"] = "🚀 GitHub Trending\n" + "\n".join(lines)
    if oil:
        parts = ["92# %s | 95# %s | 98# %s | 0#柴油 %s" % (
            oil["prices"].get("92#", "?"), oil["prices"].get("95#", "?"),
            oil["prices"].get("98#", "?"), oil["prices"].get("0#柴油", "?"))]
        if oil["adjust"] and oil["rmb_t"]:
            extra = " (约 %s)" % oil["yl_range"] if oil["yl_range"] else ""
            parts.append("下次调价：%s，%s%s" % (oil["adjust"], oil["rmb_t"], extra))
        if oil["delta"]:
            parts.append("较上次记录：%s" % oil["delta"])
        sections["oil"] = "⛽ 油价（内蒙古，全国统一调价）\n" + "\n".join(parts)
    if weather:
        # open-meteo 风速单位是 km/h，先转 m/s 再套蒲福风级公式 B=(v/0.836)^(2/3)
        bft = round((weather["wind"] / 3.6 / 0.836) ** (2 / 3))
        sections["weather"] = (
            "🌤 呼和浩特天气\n今天 %d~%d°C，%s，风力 %d 级（%.0f km/h），降水概率 %d%%"
            % (weather["t_min"], weather["t_max"], weather["weather"],
               bft, weather["wind"], weather["precip"]))
    if news:
        lines = []
        for n in news:
            line = "【%s】%s — %s" % (n["feed"], n["title"], n["summary"])
            if n["link"]:
                line += "\n%s" % n["link"]
            lines.append(line)
        sections["news"] = "💡 有用新闻\n" + "\n".join(lines)

    if not sections:
        print("🏁 Step 6: 今日无内容，不发送")
        if news_ok:
            seen = seen_acc
        _prune_and_save(seen)
        return

    # ---- 语音（尽力而为）----
    if NEWS_CONFIG["voice_enabled"]:
        if _DEADLINE - time.monotonic() > _MIN_REMAINING:
            mp3_path = _tts_to_mp3(_build_script(sections))
            if mp3_path:
                _send_telegram_audio(mp3_path, "每日速递 %s" % datetime.date.today().strftime("%Y-%m-%d"))
        else:
            print("  ⏰ 时间不足，跳过语音")

    # ---- 文字（_send_telegram_raw 负责分段；纯文本避免 Markdown 解析失败）----
    body = "\n\n".join(sections.values())
    prefix = "📰 每日速递 %s" % datetime.date.today().strftime("%Y-%m-%d")
    _send_telegram_raw(body, parse_mode=None, prefix=prefix)

    # ---- 账本提交 ----
    # 新闻候选只有 LLM 成功才落盘（失败时明天重试，不烧候选）；
    # trending/油价的记录在主 seen 里，始终提交
    if news_ok:
        seen = seen_acc
    _prune_and_save(seen)

    print(f"\n🏁 Step 6 完成: {list(sections.keys())} 板块已发送")
