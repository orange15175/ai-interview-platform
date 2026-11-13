"""仓储层：所有 SQL 都收在这里，服务层只描述业务顺序。

约定：
- 函数都接收调用方的 Session，不自己开事务（提交边界属于服务层）；
- 查询一律带 owner/session 条件，越权靠 SQL 而不是 if 判断；
- 幂等由唯一约束兜底，撞约束时抛 IntegrityError，由服务层判定语义。
"""

from app.repositories import answers, materials, questions, sessions, visitors

__all__ = ["answers", "materials", "questions", "sessions", "visitors"]
