"""
OSS 工具函数 — 读写 JSON/文本/文件，文件存在检查。
"""

import json
import time

import oss2

from config import OSS_CONFIG


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


def _safe_title(title):
    """生成安全的文件名/论文 ID。"""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
