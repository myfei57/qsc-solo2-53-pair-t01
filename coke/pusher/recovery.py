"""推焦安全停住后的人工恢复状态。

安全停住是“部分失败”：导焦栅仍锁着、炉号和目标仍明确，但重新起推必须先
有人确认。恢复文档随状态落盘；同一个确认令牌重复触发恢复时返回同一结果，
不重复执行退杆和起推。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..store.jsonfile import JsonStore

DOC_ID = "push_recovery"


class PushRecovery:
    """管理待恢复推焦行程的确认与幂等恢复。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def state(self) -> dict:
        raw = self._store.read(DOC_ID)
        return raw if isinstance(raw, dict) else {}

    def active(self) -> bool:
        return bool(self.state().get("active"))

    def require_clear(self) -> None:
        if self.active():
            raise InterlockError("存在需人工确认后恢复的推焦停住行程", component="pusher")

    def create(
        self,
        *,
        op_id: str,
        oven: str,
        target_mm: float,
        dt: float,
        policy: str,
        position_mm: float,
        reason: str,
    ) -> dict:
        """登记一次部分失败；重复登记同一条停住不覆盖原确认。"""

        current = self.state()
        if current.get("active") and current.get("op_id") == op_id:
            return current
        if current.get("active"):
            raise InterlockError("已有另一炉推焦停住待恢复", component="pusher")
        state = {
            "active": True,
            "op_id": op_id,
            "oven": oven,
            "target_mm": float(target_mm),
            "dt": float(dt),
            "deviation_policy": policy,
            "stopped_position_mm": float(position_mm),
            "stop_reason": reason,
            "acknowledged": False,
            "acknowledged_by": "",
            "resume_token": "",
            "resuming": False,
            "finished": False,
            "attempts": 0,
            "last_report": None,
        }
        self._store.write(DOC_ID, state)
        return state

    def acknowledge(self, op_id: str, operator: str) -> dict:
        """人工确认停住原因，返回恢复令牌；重复确认同一操作幂等。"""

        if not operator:
            raise InterlockError("恢复推焦必须填写确认人", component="pusher")
        state = self._require_active(op_id)
        if state.get("acknowledged"):
            if state.get("acknowledged_by") != operator:
                raise InterlockError("该停住行程已由其他人员确认", component="pusher")
            return state
        state["acknowledged"] = True
        state["acknowledged_by"] = operator
        state["resume_token"] = f"resume-{op_id}"
        self._store.write(DOC_ID, state)
        return state

    def request_resume(self, op_id: str, token: str) -> dict:
        """校验并占用一次恢复动作；已完成的重复请求直接返回缓存。"""

        state = self._require_active(op_id)
        if not state.get("acknowledged"):
            raise InterlockError("重新起推前必须有人确认停住原因", component="pusher")
        if token != state.get("resume_token"):
            raise InterlockError("恢复令牌无效或不属于该停住行程", component="pusher")
        if state.get("finished"):
            return {"idempotent": True, "state": state}
        if state.get("resuming"):
            raise InterlockError("同一恢复动作正在执行，重复触发已忽略", component="pusher")
        state["resuming"] = True
        state["attempts"] = int(state.get("attempts", 0)) + 1
        self._store.write(DOC_ID, state)
        return {"idempotent": False, "state": state}

    def finish_resume(self, op_id: str, report: dict, ok: bool) -> dict:
        """写入恢复结果；成功后关闭待恢复状态但保留幂等结果。"""

        state = self._require_active(op_id)
        state["resuming"] = False
        state["last_report"] = report
        if not report.get("ok"):
            state["acknowledged"] = False
            state["acknowledged_by"] = ""
            state["resume_token"] = ""
        if report.get("ok"):
            state["active"] = False
            state["finished"] = True
        self._store.write(DOC_ID, state)
        return state

    def _require_active(self, op_id: str) -> dict:
        state = self.state()
        if not state.get("active") and not state.get("finished"):
            raise InterlockError("没有待恢复的推焦停住行程", component="pusher")
        if state.get("op_id") != op_id:
            raise InterlockError("恢复请求与停住行程不匹配", component="pusher")
        if not state.get("active"):
            return state
        return state
