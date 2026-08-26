"""
论文自动处理系统 - 阿里云函数计算入口

定时触发配置 (Asia/Shanghai):
  00:00/08:00/16:00 → {"step": "download_extract"}   下载论文 + 抽取文本
  02:00/10:00/18:00 → {"step": "summarize"}           大模型总结 + Telegram 推送
  周一 08:00        → {"step": "weekly_summary"}      每周总结
  每天 23:40        → {"step": "breakdown"}           目标拆解
  每天 07:00        → {"step": "daily_digest"}        每日速递 (GitHub/油价/天气/新闻 + 语音)
  (ccf_check 手动触发)

本地测试:
  python main.py download_extract
  python main.py summarize
  python main.py weekly_summary
  python main.py breakdown
  python main.py daily_digest
  python main.py ccf_check
"""

import datetime
import json
import os
import sys

from step1_download import step1_download_and_extract
from step2_summarize import step2_summarize
from step4_weekly import step4_weekly_summary
from step5_breakdown import step5_breakdown
from step6_digest import step6_daily_digest
from ccf_check import step_ccf_check


# 步骤路由表: step → func
_STEP_MAP = {
    "download_extract": step1_download_and_extract,
    "summarize":        step2_summarize,
    "weekly_summary":   step4_weekly_summary,
    "breakdown":        step5_breakdown,
    "daily_digest":     step6_daily_digest,
    "ccf_check":        step_ccf_check,
}


def handler(event, context=None):
    """
    阿里云函数计算统一入口。

    event["step"] 指定步骤:
      "download_extract"  下载论文 + 抽取文本
      "summarize"         大模型总结 + Telegram 推送
      "weekly_summary"    每周总结
      "breakdown"         目标拆解
      "daily_digest"      每日速递 (GitHub/油价/天气/新闻 + 语音)
      "ccf_check"         CCF 投稿截止提醒
    """
    if isinstance(event, bytes):
        event = event.decode("utf-8")
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            event = {"step": event}

    # ── HTTP 触发：FC3 HTTP 触发器事件含 rawPath / requestContext ──
    if isinstance(event, dict) and ("rawPath" in event or "requestContext" in event):
        try:
            import webapp  # 延迟导入：定时任务冷启动不加载 flask/lark_oapi
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            return {
                "statusCode": 500,
                "headers": {"content-type": "text/plain; charset=utf-8"},
                "body": f"webapp 加载失败: {e}",
                "isBase64Encoded": False,
            }
        return webapp.handle_http_event(event)

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

    func = _STEP_MAP[step]

    try:
        func()
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

    required = ["OSS_ACCESS_KEY", "OSS_SECRET_KEY", "OPENROUTER_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"⚠️ 未设置: {missing}")
        print("  在 FC 控制台配置环境变量，或本地 export 后运行\n")

    handler({"step": step})
