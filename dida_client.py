"""
滴答清单 Open API 封装（OAuth2 Bearer Token，token 缓存到 OSS）。
官方文档: https://dida365.com/openapi
"""

import datetime
import json

import requests

from config import DIDA_CONFIG
from oss_utils import _oss_load_json, _oss_save_json

# Token 缓存 key
_TOKEN_KEY = "dida_token.json"

# Open API 基础路径
_BASE = "https://api.dida365.com/open/v1"


def _utc_str(dt=None):
    """将 datetime（北京时间）转为滴答清单 UTC 字符串 "yyyy-MM-ddTHH:mm:ss+0000"。"""
    if dt is None:
        dt = datetime.datetime.now()
    utc = dt - datetime.timedelta(hours=8)
    return utc.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _parse_token(data):
    """从 OAuth2 token 响应中提取 access_token/refresh_token/expires_at。"""
    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 0)  # seconds
    expires_at = 0
    if expires_in:
        expires_at = int(datetime.datetime.now().timestamp()) + expires_in - 300  # 提前 5 分钟刷新
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "updated": datetime.datetime.now().isoformat(),
    }


def exchange_code_for_token(code):
    """用 OAuth2 authorization code 换取 token，缓存到 OSS。

    供外部（webapp /auth 命令）调用。
    Returns:
        (True, "ok") 或 (False, "error message")
    """
    try:
        resp = requests.post(
            "https://dida365.com/oauth/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "scope": "tasks:read tasks:write",
                "redirect_uri": "https://run.gzhlaker.cc/telegram_webhook",
            },
            auth=(DIDA_CONFIG["client_id"], DIDA_CONFIG["client_secret"]),
            timeout=15,
        )
        if resp.status_code != 200:
            return (False, f"Token 交换失败 ({resp.status_code}): {resp.text[:300]}")

        token = _parse_token(resp.json())
        if not token["access_token"]:
            return (False, "Token 响应中缺少 access_token")

        _oss_save_json(_TOKEN_KEY, token)
        print(f"✅ Dida OAuth token 已缓存到 OSS")
        return (True, "授权成功")
    except Exception as e:
        return (False, str(e))


def get_auth_url():
    """生成 OAuth2 授权 URL，用户打开后授权即可获得 code。"""
    cid = DIDA_CONFIG["client_id"]
    return (
        "https://dida365.com/oauth/authorize"
        f"?scope=tasks%3Aread%20tasks%3Awrite"
        f"&client_id={cid}"
        "&state=dida_oauth"
        "&redirect_uri=https%3A%2F%2Frun.gzhlaker.cc%2Ftelegram_webhook"
        "&response_type=code"
    )


def _load_token():
    """从 OSS 加载缓存的 token。不存在或过期时尝试刷新。"""
    cached = _oss_load_json(_TOKEN_KEY, {})
    access = cached.get("access_token", "")
    refresh = cached.get("refresh_token", "")
    expires_at = cached.get("expires_at", 0)

    # 未过期直接返回
    if access and expires_at > datetime.datetime.now().timestamp():
        return access

    # 尝试刷新
    if refresh:
        try:
            resp = requests.post(
                "https://dida365.com/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                auth=(DIDA_CONFIG["client_id"], DIDA_CONFIG["client_secret"]),
                timeout=15,
            )
            if resp.status_code == 200:
                token = _parse_token(resp.json())
                if token["access_token"]:
                    # 保留旧 refresh_token（如果服务端没返回新的）
                    if not token["refresh_token"]:
                        token["refresh_token"] = refresh
                    _oss_save_json(_TOKEN_KEY, token)
                    print(f"🔄 Dida token 已刷新")
                    return token["access_token"]
        except Exception:
            pass

    return ""


class DidaList:
    """滴答清单 Open API 操作封装。"""

    def __init__(self):
        self._token = _load_token()
        if not self._token:
            print("⚠️  Dida token 未配置或已过期，请先通过 /dida_auth 授权")

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def is_ready(self):
        """检查 token 是否可用。"""
        return bool(self._token)

    # ---- 任务操作 ----

    def createTask(self, title, projectId="", parentId="", columnId="",
                   tags=None, priority=0, startDate=None, dueDate=None, content=""):
        """创建任务。

        Returns:
            (isSuccess, msg)
        """
        if not self._token:
            return (False, "未授权，请先 /dida_auth")

        body = {"title": title, "projectId": projectId, "priority": priority}

        if content:
            body["content"] = content
        if tags:
            body["tags"] = tags
        if startDate:
            body["startDate"] = _utc_str(startDate)
            body["isAllDay"] = True if not dueDate else False
        if dueDate:
            body["dueDate"] = _utc_str(dueDate)
            body["isAllDay"] = True

        try:
            resp = requests.post(
                f"{_BASE}/task",
                headers=self._headers,
                json=body,
                timeout=15,
            )
            data = resp.json()
            if resp.status_code in (200, 201):
                task_id = data.get("id", "")
                # 如果有 parentId，关联父子关系
                if parentId and task_id:
                    self._link_parent(parentId, projectId, task_id)
                return (True, task_id)
            return (False, data.get("errorMessage", resp.text[:200]))
        except Exception as e:
            return (False, str(e))

    def _link_parent(self, parentId, projectId, taskId):
        """通过更新任务关联父子关系（Open API 不直接支持 batch/taskParent）。"""
        # Open API 不支持直接创建父子关系；作为 subtask 创建
        pass

    def completeTask(self, taskId, projectId):
        """标记任务为完成。

        Returns:
            (isSuccess, msg)
        """
        if not self._token:
            return (False, "未授权")

        try:
            resp = requests.post(
                f"{_BASE}/project/{projectId}/task/{taskId}/complete",
                headers=self._headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return (True, f"Task {taskId} completed")
            data = resp.json() if resp.text else {}
            return (False, data.get("errorMessage", resp.text[:200]))
        except Exception as e:
            return (False, str(e))

    def getFilterTask(self, title=None, projectId=None, parentId=None,
                      columnId=None, tags=None, priority=None,
                      startDate=None, dueDate=None):
        """过滤任务（服务端过滤，仅支持 tag / status / projectIds）。

        注意：Open API 不支持按 title/parentId/columnId/date 过滤，
        这些条件在客户端二次过滤。status=0 只查未完成任务。
        """
        if not self._token:
            return []

        body = {"status": [0]}  # 只查未完成

        if projectId:
            body["projectIds"] = [projectId]
        if tags:
            body["tag"] = tags  # 服务端 AND 匹配；单 tag 等价于 filter
        # priority 在 Open API filter 中与客户端定义一致
        if priority is not None:
            body["priority"] = [priority]

        try:
            resp = requests.post(
                f"{_BASE}/task/filter",
                headers=self._headers,
                json=body,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"⚠️  filter 失败: {resp.text[:200]}")
                return []
            tasks = resp.json() if isinstance(resp.json(), list) else []

            # 客户端二次过滤（Open API 不支持的条件）
            if title:
                tasks = [t for t in tasks if t.get("title") == title]
            if parentId:
                tasks = [t for t in tasks if t.get("parentId") == parentId]
            if columnId:
                tasks = [t for t in tasks if t.get("columnId") == columnId]
            if startDate and not dueDate:
                tasks = [t for t in tasks if t.get("startDate", "").startswith(startDate.strftime("%Y-%m-%d"))]
            elif dueDate and not startDate:
                tasks = [t for t in tasks if t.get("dueDate", "").startswith(dueDate.strftime("%Y-%m-%d"))]

            return tasks
        except Exception as e:
            print(f"⚠️  filter 异常: {e}")
            return []

    def getCompletedTasks(self, projects=None, startTime=None, endTime=None, limit=200):
        """获取已完成任务。

        Args:
            projects: 清单 ID 列表，None 表示不过滤
            startTime: 开始时间 (datetime)
            endTime: 结束时间 (datetime)
            limit: 数量限制
        """
        if not self._token:
            return []

        body = {}
        if projects:
            body["projectIds"] = projects
        if startTime:
            body["startDate"] = _utc_str(startTime)
        if endTime:
            body["endDate"] = _utc_str(endTime)

        try:
            resp = requests.post(
                f"{_BASE}/task/completed",
                headers=self._headers,
                json=body,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"⚠️  completed 失败: {resp.text[:200]}")
                return []
            tasks = resp.json()
            return tasks[:limit] if isinstance(tasks, list) else []
        except Exception as e:
            print(f"⚠️  completed 异常: {e}")
            return []

    # ---- 查询方法 ----

    def getProjects(self):
        """获取所有清单。"""
        if not self._token:
            return []
        try:
            resp = requests.get(f"{_BASE}/project", headers=self._headers, timeout=15)
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    def getColumns(self, projectId=None):
        """获取指定清单的分组。"""
        if not self._token or not projectId:
            return []
        try:
            resp = requests.get(
                f"{_BASE}/project/{projectId}/column",
                headers=self._headers,
                timeout=15,
            )
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    def getHabits(self):
        """获取所有习惯。"""
        if not self._token:
            return []
        try:
            resp = requests.get(f"{_BASE}/habit", headers=self._headers, timeout=15)
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    # ---- 标签管理 ----

    def getTags(self):
        """获取所有标签。"""
        if not self._token:
            return []
        try:
            resp = requests.get(f"{_BASE}/tag", headers=self._headers, timeout=15)
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    def createTag(self, name):
        """创建标签。"""
        if not self._token:
            return (False, "未授权")
        try:
            resp = requests.post(
                f"{_BASE}/tag",
                headers=self._headers,
                json={"name": name, "label": name},
                timeout=15,
            )
            if resp.status_code == 200:
                return (True, resp.json())
            return (False, resp.text[:200])
        except Exception as e:
            return (False, str(e))

    # ---- 辅助 ----

    def genProjectIDJson(self):
        projects = self.getProjects()
        _oss_save_json("dida_projects.json", projects)

    def genColumnIDJson(self):
        columns = self.getColumns()
        _oss_save_json("dida_columns.json", columns)

    def genTaskIDJson(self):
        tasks = self.getFilterTask()
        _oss_save_json("dida_tasks.json", tasks)
