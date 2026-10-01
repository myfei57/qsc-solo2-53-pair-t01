"""起推炉序策略。

规则定死为按炉号数字升序，不接受班组排队。首个炉号建立本班序列；之后只能
推更大的炉号。部分失败的炉号由恢复接口重试，不占用新炉号。
"""

from __future__ import annotations

import re

from ..errors import InterlockError
from ..store.jsonfile import JsonStore

DOC_ID = "push_schedule"
POLICY = "oven_number_ascending"
_NUMBER_RE = re.compile(r"(\d+)$")


class PushSchedule:
    """按炉号顺序发放推焦许可。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def state(self) -> dict:
        raw = self._store.read(DOC_ID)
        if not isinstance(raw, dict):
            return {"policy": POLICY, "last_completed_oven": "", "last_completed_number": None, "active_oven": ""}
        return {
            "policy": POLICY,
            "last_completed_oven": str(raw.get("last_completed_oven", "")),
            "last_completed_number": raw.get("last_completed_number"),
            "active_oven": str(raw.get("active_oven", "")),
        }

    def begin(self, oven: str, *, crew: str | None = None) -> dict:
        """校验本次起推顺序并占用当前炉号。"""

        if crew:
            raise InterlockError("起推顺序已定为按炉号升序，不按班组排队", component="schedule")
        number = self._oven_number(oven)
        state = self.state()
        last = state["last_completed_number"]
        active = state["active_oven"]
        if active and active != oven:
            raise InterlockError(f"炉号 {active} 的推焦尚未闭环，不能切换到 {oven}", component="schedule")
        if last is not None and number <= int(last):
            raise InterlockError(f"起推必须按炉号升序：上一炉为 {state['last_completed_oven']}", component="schedule")
        state["active_oven"] = oven
        self._store.write(DOC_ID, state)
        return state

    def complete(self, oven: str) -> dict:
        """正常完成当前炉号。"""

        state = self.state()
        active = state["active_oven"]
        if active and active != oven:
            raise InterlockError("完成炉号与当前占用炉号不一致", component="schedule")
        state["active_oven"] = ""
        state["last_completed_oven"] = oven
        state["last_completed_number"] = self._oven_number(oven)
        self._store.write(DOC_ID, state)
        return state

    def fail_open(self, oven: str) -> dict:
        """部分失败保留当前炉号，供恢复接口重试。"""

        state = self.state()
        if state["active_oven"] and state["active_oven"] != oven:
            raise InterlockError("停住炉号与调度占用炉号不一致", component="schedule")
        state["active_oven"] = oven
        self._store.write(DOC_ID, state)
        return state

    def release(self, oven: str) -> dict:
        """释放尚未真正起推的炉号占用（例如导焦栅对位失败）。"""

        state = self.state()
        if state["active_oven"] == oven:
            state["active_oven"] = ""
            self._store.write(DOC_ID, state)
        return state

    def clear(self) -> bool:
        """交付测试时清空炉序文档。"""

        return self._store.remove(DOC_ID)

    @staticmethod
    def _oven_number(oven: str) -> int:
        if not isinstance(oven, str) or not oven:
            raise InterlockError("炉号必须是非空字符串", component="schedule")
        match = _NUMBER_RE.search(oven)
        if match is None:
            raise InterlockError("炉号必须以可排序数字结尾", component="schedule")
        return int(match.group(1))
