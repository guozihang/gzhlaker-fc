"""
滴答清单 API 完整封装（适配 FC 环境：OSS 替代本地文件，print 替代 loguru）。
"""

import datetime
import json
import random

import requests

from config import DIDA_CONFIG
from oss_utils import _oss_load_json, _oss_save_json


_DIDA_DEVICE = json.dumps({
    "platform": "web", "os": "Windows 10",
    "device": "Chrome 86.0.4240.198", "name": "",
    "version": 4130, "id": "6732f9fd4557ba2ce15c00eb",
    "channel": "website", "campaign": "", "websocket": "",
})


# ---- createTask 模板（硬编码替代本地文件） ----
_DIDA_TASK_TEMPLATE = {
    "title": "", "projectId": "", "parentId": "", "columnId": "",
    "tags": [], "priority": 0, "startDate": None, "dueDate": None,
    "id": "", "createdTime": "", "modifiedTime": "", "content": "",
    "kind": "TEXT", "isFloating": False, "reminders": [], "exDate": [],
    "repeatFlag": "", "sortOrder": 0, "progress": 0, "assignee": None,
    "isAllDay": True, "reminderTime": "", "pomodoroSummaries": [],
    "repeateFromTaskId": "", "focusSummaries": [],
}

_DIDA_UPDATE_TASK_TEMPLATE = {
    "add": [], "update": [], "delete": [], "addAttachments": [],
    "updateAttachments": [], "deleteAttachments": [],
}


class DidaAPI:
    """滴答清单 API 端点"""

    def __init__(self):
        self.getProjects = "https://api.dida365.com/api/v2/projects"
        self.createTask = "https://api.dida365.com/api/v2/batch/task"
        self.getHabits = "https://api.dida365.com/api/v2/habits"
        self.getUTCTimetable = "https://api.dida365.com/api/v1/course/timetable"
        self.getColumns = "https://api.dida365.com/api/v2/column?from=0"
        self.getCompleteTasks = "https://api.dida365.com/api/v2/project/{}/completed/?from={}&to={}&limit={}"
        self.createHabit = "https://api.dida365.com/api/v2/habits/batch"
        self.createSubTask = "https://api.dida365.com/api/v2/batch/taskParent"
        self.getColumnsInProject = "https://api.dida365.com/api/v2/column/project/{}"
        self.getAllInfo = "https://api.dida365.com/api/v2/batch/check/0"
        self.login = "https://api.dida365.com/api/v2/user/signon?wc=true&remember=true"


class DidaList:
    """滴答清单操作封装，cookie 缓存到 OSS。"""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; rv2.0.1) Gecko/20100101 Firefox/4.0.1",
            "authority": "api.dida365.com",
            "referer": "https://dida365.com/webapp/",
            "origin": "https://dida365.com",
            "x-device": _DIDA_DEVICE,
        }
        self.__API = DidaAPI()

        # 尝试从 OSS 加载缓存的 cookie
        cached = _oss_load_json("dida_cookie.json", {})
        cookie = cached.get("cookie", "")
        if cookie:
            self.headers["cookie"] = cookie
            self._cookie_loaded = True
        else:
            self._cookie_loaded = False

    def updateCookie(self):
        """登录滴答清单，cookie 缓存到 OSS。"""
        resp = requests.post(
            url=self.__API.login,
            headers=self.headers,
            json={"password": DIDA_CONFIG["password"], "phone": DIDA_CONFIG["phone"]},
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"滴答清单登录失败: {resp.content}")
        print(f"✅ 滴答清单登录成功")

        cookie = ""
        for name, value in resp.cookies.items():
            cookie += f"{name}={value};"

        self.headers["cookie"] = cookie
        _oss_save_json("dida_cookie.json", {"cookie": cookie, "updated": datetime.datetime.now().isoformat()})
        print(f"  ↳ cookie 已缓存到 OSS")

    # ---- 工具方法 ----

    def __getUTCTime(self, t=None):
        """生成滴答清单使用的 UTC 格式化时间字符串。"""
        if t is None:
            t = datetime.datetime.now()
        template = "%Y-%m-%dT%H:%M:%S.000+0000"
        utc_time = t - datetime.timedelta(hours=8)
        return utc_time.strftime(template)

    def __getTimeFromTimeStamp(self, timestamp):
        """根据时间戳生成 UTC 时间字符串。"""
        template = "%Y-%m-%dT%H:%M:%S.000+0000"
        target_time = datetime.datetime.fromtimestamp(timestamp)
        utc_time = target_time - datetime.timedelta(hours=8)
        return utc_time.strftime(template)

    def __generateID(self, num=24):
        """生成滴答清单格式的 ID。"""
        H = "abcdef0123456789" * 3
        return "".join(random.sample(H, num))

    # ---- 核心操作 ----

    def createTask(self, title, projectId="", parentId="", columnId="",
                   tags=None, priority=0, startDate=None, dueDate=None, content=""):
        """创建任务。

        Returns:
            (isSuccess, msg)
        """
        if tags is None:
            tags = []

        task = dict(_DIDA_TASK_TEMPLATE)
        payload = dict(_DIDA_UPDATE_TASK_TEMPLATE)
        payload["add"] = []
        payload["update"] = []
        payload["delete"] = []
        payload["addAttachments"] = []
        payload["updateAttachments"] = []
        payload["deleteAttachments"] = []

        time_str = self.__getUTCTime()

        task["title"] = title
        task["projectId"] = projectId
        task["parentId"] = parentId
        task["columnId"] = columnId
        task["tags"] = tags
        task["priority"] = priority
        task["startDate"] = None if not startDate else self.__getUTCTime(startDate)
        task["dueDate"] = None if not dueDate else self.__getUTCTime(dueDate)
        task["id"] = self.__generateID()
        task["createdTime"] = time_str
        task["modifiedTime"] = time_str
        task["content"] = content

        payload["add"].append(task)

        res = requests.post(
            url=self.__API.createTask,
            headers=self.headers,
            json=payload,
            timeout=15,
        )

        if res.status_code == 200:
            if parentId:
                ok, msg = self._createSubTask(parentId, projectId, task["id"])
                if not ok:
                    return (False, msg)
            return (True, f"create task successfully: {res.content}")
        else:
            return (False, f"create task failed: {res.content}")

    def _createSubTask(self, parentId, projectId, taskId):
        """创建子任务关联。"""
        payload = [{"parentId": parentId, "projectId": projectId, "taskId": taskId}]

        res = requests.post(
            url=self.__API.createSubTask,
            headers=self.headers,
            json=payload,
            timeout=15,
        )

        if res.status_code == 200:
            return (True, f"create subtask successfully: {res.content}")
        else:
            return (False, f"create subtask failed: {res.content}")

    def getFilterTask(self, title=None, projectId=None, parentId=None,
                      columnId=None, tags=None, priority=None,
                      startDate=None, dueDate=None):
        """根据过滤条件返回符合条件的未完成任务。"""

        def _filter(task):
            if title is not None and task.get("title") != title:
                return False
            if projectId is not None and task.get("projectId") != projectId:
                return False
            if parentId is not None and task.get("parentId") != parentId:
                return False
            if columnId is not None and task.get("columnId") != columnId:
                return False
            if tags is not None:
                task_tags = task.get("tags") or []
                if not (set(task_tags) & set(tags)):
                    return False
            if priority is not None and task.get("priority") != priority:
                return False

            if startDate is not None and dueDate is not None:
                if "startDate" in task and "dueDate" in task:
                    ts = datetime.datetime.strptime(task["startDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    ts += datetime.timedelta(hours=8)
                    td = datetime.datetime.strptime(task["dueDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if not (startDate <= ts <= td <= dueDate):
                        return False
                else:
                    return False
            elif startDate is not None:
                if "startDate" in task:
                    td = datetime.datetime.strptime(task["startDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if td.date() != startDate.date():
                        return False
                else:
                    return False
            elif dueDate is not None:
                if "dueDate" in task:
                    td = datetime.datetime.strptime(task["dueDate"], "%Y-%m-%dT%H:%M:%S.000+0000")
                    td += datetime.timedelta(hours=8)
                    if td.date() != dueDate.date():
                        return False
                else:
                    return False

            return True

        res = requests.get(url=self.__API.getAllInfo, headers=self.headers, timeout=15)
        all_tasks = res.json().get("syncTaskBean", {}).get("update", [])
        return [t for t in all_tasks if _filter(t)]

    # ---- 查询方法 ----

    def getProjects(self):
        """获取所有清单。"""
        res = requests.get(url=self.__API.getProjects, headers=self.headers, timeout=15)
        return res.json()

    def getColumns(self, projectId=None):
        """获取分组，可指定清单过滤。"""
        res = requests.get(url=self.__API.getColumns, headers=self.headers, timeout=15)
        columns = res.json().get("update", [])

        if projectId is not None:
            return [c for c in columns if c.get("projectId") == projectId]
        return columns

    def completeTask(self, taskId, projectId):
        """标记任务为完成。

        从 getAllInfo 获取任务完整数据，更新 status=2 后通过 batch update 提交。
        """
        # 1. 获取任务完整数据
        res = requests.get(url=self.__API.getAllInfo, headers=self.headers, timeout=15)
        all_tasks = res.json().get("syncTaskBean", {}).get("update", [])
        task = next((t for t in all_tasks if t.get("id") == taskId), None)
        if not task:
            return (False, f"Task {taskId} not found")

        # 2. 修改状态为已完成
        task["status"] = 2
        task["completedTime"] = self.__getUTCTime()
        task["modifiedTime"] = self.__getUTCTime()

        # 3. 通过 batch update 提交
        payload = dict(_DIDA_UPDATE_TASK_TEMPLATE)
        payload["update"] = [task]
        payload["add"] = []
        payload["delete"] = []
        payload["addAttachments"] = []
        payload["updateAttachments"] = []
        payload["deleteAttachments"] = []

        res = requests.post(
            url=self.__API.createTask,
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        if res.status_code == 200:
            return (True, f"Task {taskId} completed")
        return (False, f"Complete task failed: {res.content}")

    def getHabits(self):
        """获取所有习惯。"""
        res = requests.get(url=self.__API.getHabits, headers=self.headers, timeout=15)
        return res.json()

    def getCompletedTasks(self, projects=None, startTime=None, endTime=None, limit=200):
        """获取已完成任务。

        Args:
            projects: 清单 ID 列表，None 表示所有
            startTime: 开始时间 (datetime)
            endTime: 结束时间 (datetime)
            limit: 数量限制
        """
        proj_str = ",".join(projects) if projects else "all"

        from_str = ""
        to_str = ""
        if startTime:
            from_str = startTime.strftime("%Y-%m-%d") + "%20" + startTime.strftime("%H:%M:%S")
        if endTime:
            to_str = endTime.strftime("%Y-%m-%d") + "%20" + endTime.strftime("%H:%M:%S")

        url = self.__API.getCompleteTasks.format(proj_str, from_str, to_str, limit)
        res = requests.get(url=url, headers=self.headers, timeout=15)

        tasks = res.json()
        return tasks if isinstance(tasks, list) else []

    def genProjectIDJson(self):
        """将清单 ID 保存到 OSS。"""
        projects = self.getProjects()
        _oss_save_json("dida_projects.json", projects)

    def genColumnIDJson(self):
        """将分组 ID 保存到 OSS。"""
        columns = self.getColumns()
        _oss_save_json("dida_columns.json", columns)

    def genTaskIDJson(self):
        """将未完成任务 ID 保存到 OSS。"""
        tasks = self.getFilterTask()
        _oss_save_json("dida_tasks.json", tasks)
