"""
Step 1: 从 arxiv 下载论文并抽取文本内容。
定时触发: 00:00 / 08:00 / 16:00
"""

import datetime
import os
import time

import arxiv
import oss2
import requests

from oss_utils import (
    _oss_client, _oss_load_json, _oss_save_json,
    _oss_file_exists, _oss_upload_file, _safe_title,
)
from pdf_utils import _extract_pdf_text


def step1_download_and_extract():
    """
    Step 1: 从 arxiv 下载论文并抽取文本内容。

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

                # 过滤旧论文：只保留最近 30 天
    if result.published.replace(tzinfo=None) < datetime.datetime.now() - datetime.timedelta(days=30):
        continue

                # 生成安全的文件名（也用作论文唯一 ID）
                safe_title = _safe_title(result.title)

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
