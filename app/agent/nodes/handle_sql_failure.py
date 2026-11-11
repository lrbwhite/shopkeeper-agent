"""
SQL 修正失败节点

当 validate_sql 连续报错且修正次数达到上限时进入本节点
它不抛异常，而是把最后一次 SQL 和数据库错误包装成一条可读消息返回给前端
保证链路可以终止，同时保留定位问题所需的信息
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger


async def handle_sql_failure(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
):
    """重试耗尽后返回失败信息，而不是让异常打断流式响应"""

    writer = runtime.stream_writer
    step = "SQL修正失败"
    error = state.get("error") or "未知错误"
    retry_count = state.get("retry_count", 0)

    logger.warning(f"{step}：修正 {retry_count} 次后仍然报错 {error}")
    writer({"type": "progress", "step": step, "status": "error"})
    # 把最后一条 SQL 一起返回，前端可以展示“错在哪条 SQL 上”，而不是只看到一句报错
    writer(
        {
            "type": "error",
            "message": f"SQL 修正 {retry_count} 次后仍未通过校验：{error}",
            "sql": state.get("sql", ""),
        }
    )
    return {}
