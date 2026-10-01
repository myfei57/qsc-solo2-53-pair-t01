"""起推顺序策略。

起推顺序在系统中固定为炉号升序，不按班组排队。班组只作为审计上下文中的
执行信息，不能改变同一批次的先后次序，避免现场按人力方便跳炉。
"""

from __future__ import annotations

import re

OVEN_ORDER = "oven_number_ascending"
CREW_ORDER = "crew"
START_PUSH_ORDER = OVEN_ORDER

_OVEN_RE = re.compile(r"(\d+)$")


def oven_sort_key(oven: str) -> tuple[int, str]:
    """优先按炉号尾部数字升序，非数字名称再按字符串稳定排序。"""

    match = _OVEN_RE.search(str(oven))
    if match:
        return (int(match.group(1)), str(oven))
    return (10**12, str(oven))


class PushSchedule:
    """固定炉号升序的起推队列。"""

    def plan(self, ovens: list[str], crew: str = "") -> dict:
        """返回去重后的炉号升序队列；crew 不参与排序。"""

        ordered = sorted(dict.fromkeys(str(oven) for oven in ovens), key=oven_sort_key)
        return {
            "start_push_order": START_PUSH_ORDER,
            "crew_order_ignored": True,
            "crew": str(crew),
            "ovens": ordered,
            "reason": "起推顺序固定按炉号升序，班组仅记录、不参与排队",
        }
