"""模型输出解析和校验工具，避免直接信任自由文本。

和 graph.py 里的 _parse_json 的区别：
    _parse_json        解析失败返回 {}，调用方静默降级（旧流程用）
    parse_json_object  解析失败抛异常，调用方必须显式处理（新流程用）

为什么新流程要更严格？
    旧流程失败就按 0 分算，用户顶多看到"0 分"；
    新流程要把评分写进报告、算平均分，一个静默的 0 分会污染整份报告，
    所以宁可报错让调用方决定怎么处理。
"""

import json
import re
from typing import Any


class ModelOutputError(ValueError):
    """表示模型返回的内容无法转换为约定结构。

    继承 ValueError 的意义：调用方可以 `except ValueError` 一次性捕获
    所有"值不对"的问题，也可以精确捕获这个类型只处理模型输出问题。
    """


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象，无法解析或根类型错误时给出明确异常。

    为什么要正则而不是直接 json.loads？
        因为模型即使被要求"只输出 JSON"，仍可能加 ```json 包裹，
        或前面来一句"好的，以下是评分："。所以先抠出第一个 {...} 再解析。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)  # DOTALL 让 . 匹配换行
    if not match:
        raise ModelOutputError("模型没有返回 JSON 对象")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        # from exc 保留原始异常链，调试时能看到到底哪里格式错了
        raise ModelOutputError("模型返回的 JSON 格式错误") from exc

    # 模型可能返回 [1, 2, 3] 这种合法 JSON 但不是对象，单独拦一层
    if not isinstance(data, dict):
        raise ModelOutputError("模型返回的 JSON 不是对象")

    return data


def clamp_score(value: Any) -> int:
    """把可转换分数整理成 0～100 的整数，限制异常数值范围。

    三种异常输入都会被拦下：
        "85分"  -> int() 抛 ValueError
        None    -> int() 抛 TypeError
        {}      -> int() 抛 TypeError
    转换为 0 静默处理是不行的——报告里一个假 0 分比报错更糟。"""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError) as exc:
        raise ModelOutputError("模型返回了非法分数") from exc
