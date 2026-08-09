"""
CCF 投稿截止提醒 — 检查 CCF 会议投稿截止日期并发送 Telegram 提醒。
"""

import re
import subprocess

from channels import _send_telegram_raw


def step_ccf_check():
    """检查 CCF 会议投稿截止日期并发送提醒。"""
    print("=" * 50)
    print("📅 CCF 投稿截止提醒")
    print("=" * 50)

    result = subprocess.run(
        ["python", "-m", "ccfddl"],
        capture_output=True, text=True,
    )

    deadlines = []
    for line in result.stdout.split("\n"):
        if "https://" not in line:
            continue
        line = line.replace(" ", "")
        line = line.replace("days", "天").replace("months", "月")
        parts = line.split("│")
        try:
            match = re.search(r"'ccf'\s*:\s*'([^']*)'", parts[3])
            if match and match.group(1) == "A":
                deadlines.append(f"【CCF-{match.group(1)}】【{parts[1]}】剩余：{parts[4]}")
        except (IndexError, AttributeError):
            continue

    if deadlines:
        text = "📅 **投稿时间提醒**\n\n" + "\n".join(deadlines)
        _send_telegram_raw(text)
        print(f"✅ 已发送 CCF 提醒，共 {len(deadlines)} 个会议")
    else:
        print("✅ 无 A 类会议即将截止")
