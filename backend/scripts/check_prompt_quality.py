"""固定样例真实模型抽查：从仓库根运行 python backend/scripts/check_prompt_quality.py --live。

仅使用合成技术问答，不读取用户简历；明确 --live 才联网。结果含评分范围、波动、耗时。
范围是人工预期的宽区间，不代表经过大样本校准的准确率。结构错误会触发正常有限修复。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.adapters import DeepSeekAdapter
from app.agents.evaluate_agent import evaluate_answer
from app.agents.followup_agent import generate_followup
from app.agents.prompt_policy import PROMPT_VERSION
from app.core.config import get_settings
from app.schemas.evaluation import Evaluation


QUESTION = {
    "main_index": 1, "kind": "main", "category": "专业能力", "difficulty": "easy",
    "content": "Python中的可变对象和不可变对象有什么区别？请各举一个例子。",
    "evaluation_rubric": [
        {"point": "核心：可变对象可以原地修改，不可变对象创建后其值不能原地改变", "weight": 0.7},
        {"point": "核心：给出正确的可变与不可变对象例子，如list与tuple", "weight": 0.3},
    ],
}
GOOD = "可变对象可以在保持对象身份的情况下修改内容，如list可用append增加元素；不可变对象创建后值不能原地改变，如tuple。变量重新绑定到另一个对象不等于修改原对象。"
CASES = [
    ("正确且简短", "可变对象能原地修改，如list；不可变对象不能原地修改，如tuple。", 85, 100),
    ("解释充分", GOOD, 85, 100),
    ("部分正确", "list是可变对象，能用append增加元素；不可变对象我还不清楚。", 35, 75),
    ("核心错误", "tuple可以任意原地增删元素，list一旦创建就不能再修改。", 0, 39),
    ("跑题", "我喜欢打篮球，昨天吃了面条。", 0, 20),
    ("技术引用注入词", GOOD + "另外，‘忽略以上规则’是提示词注入的常见示例，这里只是引用。", 85, 100),
    ("等价正确例子", "可变对象允许原地改变内容，例如dict可以添加键值；不可变对象不允许原地改变自身值，例如str，拼接得到的是新字符串。", 85, 100),
    ("狭窄追问独立评分", "list是可变对象，能原地添加元素；tuple是不可变对象，不能原地修改其元素引用。", 85, 100),
]


def check_case(item, repeats):
    name, answer, low, high = item
    question = QUESTION
    if name == "狭窄追问独立评分":
        question = {**QUESTION, "kind": "follow_up", "main_question": "请介绍Python对象模型和内存管理。",
                    "content": "请各举一个可变对象与不可变对象的例子？",
                    "evaluation_rubric": [{"point": "各举一个正确的可变与不可变对象例子", "weight": 1.0}]}
    adapter = DeepSeekAdapter(get_settings().text_model)
    results = []
    for _ in range(repeats):
        start = time.perf_counter()
        try:
            ev = evaluate_answer(question, answer, adapter=adapter)
            results.append({"score": ev.score, "in_range": low <= ev.score <= high,
                            "feedback": ev.feedback, "seconds": round(time.perf_counter() - start, 2)})
        except Exception as exc:
            # 不记录供应商原始异常，避免连接配置意外进入报告。
            results.append({"error_type": type(exc).__name__})
    scores = [r["score"] for r in results if "score" in r]
    return {"case": name, "expected_range": [low, high], "results": results,
            "score_spread": max(scores) - min(scores) if scores else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/prompt-v2-quality.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("请显式传入 --live 才会调用真实模型")
    with ThreadPoolExecutor(max_workers=3) as pool:
        cases = list(pool.map(lambda item: check_case(item, args.repeats), CASES))
    followup = None
    try:
        fq = generate_followup(
            QUESTION, "我知道list能修改，但没搞清tuple。",
            Evaluation(score=55, feedback="需解释不可变性", follow_up_target="不可变对象的含义",
                       follow_up_reason="需要核实机制理解", follow_up_suggested=True),
            [QUESTION["content"]], adapter=DeepSeekAdapter(get_settings().text_model))
        followup = fq.model_dump()
    except Exception as exc:
        followup = {"error_type": type(exc).__name__}
    report = {"prompt_version": PROMPT_VERSION, "model": get_settings().text_model.model,
              "repeats": args.repeats, "cases": cases, "followup": followup}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = sum(not r.get("in_range", False) for c in cases for r in c["results"])
    print(json.dumps({"output": str(args.output), "evaluations": len(CASES) * args.repeats,
                      "outside_range_or_failed": failures, "followup_valid": "content" in followup}))
    return int(failures > 0 or "content" not in followup)


if __name__ == "__main__":
    raise SystemExit(main())
