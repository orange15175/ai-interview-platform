"""app.graph：面试流程图（阶段 3+ 正式版）。

- builder.build_interview_graph()            正常变体：prepare → 派发 → 答题循环
- builder.build_interview_graph(restore=True) 恢复变体：START 直接进 wait_answer，
  供 recovery.py 从 PostgreSQL 重建到安全等待点（§9.4）。

旧的 legacy.build_graph（三节点原型）已在阶段 5 删除；需要对照请看 git 历史。
"""

from app.graph.builder import build_interview_graph

__all__ = ["build_interview_graph"]
