"""
电商问数 Agent 图编排

使用 LangGraph 把问数智能体的各个节点串成一条可观测的执行链路
当前链路已经落地关键词抽取和多路召回，字段和指标走 Qdrant 向量检索，字段取值走 ES 全文检索
整体流程先抽取用户问题关键词，再并行召回字段 字段取值和指标信息，
随后合并召回结果 过滤候选表和指标 补充额外上下文，最后生成 校验 修正并执行 SQL
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.handle_sql_failure import handle_sql_failure
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.run_sql import run_sql
from app.agent.nodes.suggest_keywords import suggest_keywords
from app.agent.nodes.validate_sql import validate_sql
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

# StateGraph 声明整张图使用的状态结构和运行时上下文结构
graph_builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)

# SQL 校验失败后允许修正的最大次数，超过后走失败出口而不是继续循环
MAX_SQL_RETRY = 2


def traced(name: str, node: Callable[[DataAgentState, Any], Awaitable[dict]]):
    """给节点统一包一层计时和轨迹记录，避免逐个节点改代码"""

    async def wrapper(state: DataAgentState, runtime):
        started = time.perf_counter()
        try:
            result = await node(state, runtime)
        except Exception as exc:
            # 节点失败时也要留下轨迹，然后把异常继续抛给上层
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            runtime.stream_writer(
                {
                    "type": "trace",
                    "node": name,
                    "status": "error",
                    "latency_ms": latency_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        # 实时事件给前端展示链路耗时，state 里的 trace 供报告和排查使用
        runtime.stream_writer(
            {
                "type": "trace",
                "node": name,
                "status": "success",
                "latency_ms": latency_ms,
            }
        )
        return {
            **(result or {}),
            "trace": [
                {
                    "node": name,
                    "status": "success",
                    "latency_ms": latency_ms,
                    "error": None,
                }
            ],
        }

    return wrapper


def register(name: str, node: Callable[[DataAgentState, Any], Awaitable[dict]]):
    """注册节点时自动套上轨迹装饰器"""

    graph_builder.add_node(name, traced(name, node))


# 注册节点：每个节点负责问数链路中的一个清晰步骤
register("extract_keywords", extract_keywords)
register("suggest_keywords", suggest_keywords)
register("recall_column", recall_column)
register("recall_value", recall_value)
register("recall_metric", recall_metric)
register("merge_retrieved_info", merge_retrieved_info)
register("filter_metric", filter_metric)
register("filter_table", filter_table)
register("add_extra_context", add_extra_context)
register("generate_sql", generate_sql)
register("validate_sql", validate_sql)
register("correct_sql", correct_sql)
register("handle_sql_failure", handle_sql_failure)
register("run_sql", run_sql)

# 从用户问题开始，先抽取关键词作为后续检索的基础
graph_builder.add_edge(START, "extract_keywords")

# 关键词抽取后先做一次统一的三路词表扩展，再并行进入三类召回
graph_builder.add_edge("extract_keywords", "suggest_keywords")
graph_builder.add_edge("suggest_keywords", "recall_column")
graph_builder.add_edge("suggest_keywords", "recall_value")
graph_builder.add_edge("suggest_keywords", "recall_metric")

# 三路召回都完成后，再进入统一的信息合并节点
graph_builder.add_edge("recall_column", "merge_retrieved_info")
graph_builder.add_edge("recall_value", "merge_retrieved_info")
graph_builder.add_edge("recall_metric", "merge_retrieved_info")

# 合并后的候选信息继续拆成表过滤和指标过滤两条线
graph_builder.add_edge("merge_retrieved_info", "filter_table")
graph_builder.add_edge("merge_retrieved_info", "filter_metric")

# 表和指标都过滤完成后，统一补充生成 SQL 所需的上下文
graph_builder.add_edge("filter_table", "add_extra_context")
graph_builder.add_edge("filter_metric", "add_extra_context")
graph_builder.add_edge("add_extra_context", "generate_sql")
graph_builder.add_edge("generate_sql", "validate_sql")


# SQL 校验通过就直接执行；校验失败且还有修正次数就重试，否则进入失败出口
def after_validate(state: DataAgentState) -> str:
    """根据校验结果决定执行 修正 还是失败出口"""

    if state.get("error") is None:
        return "run_sql"
    if state.get("retry_count", 0) < MAX_SQL_RETRY:
        return "correct_sql"
    return "handle_sql_failure"


graph_builder.add_conditional_edges(
    source="validate_sql",
    path=after_validate,
    path_map={
        "run_sql": "run_sql",
        "correct_sql": "correct_sql",
        "handle_sql_failure": "handle_sql_failure",
    },
)
# 修正后的 SQL 必须重新校验，否则重试上限没有意义
graph_builder.add_edge("correct_sql", "validate_sql")
graph_builder.add_edge("run_sql", END)
graph_builder.add_edge("handle_sql_failure", END)

# 编译后的 graph 是对外使用的 Agent 执行入口
graph = graph_builder.compile()

# print(graph.get_graph().draw_mermaid())

if __name__ == "__main__":

    async def test():
        """本地调试关键词抽取和字段 指标 取值三路召回链路"""

        # 多路召回和上下文补全会访问 Qdrant、Embedding、ES、Meta MySQL 和 DW MySQL
        qdrant_client_manager.init()
        embedding_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()

        # Meta MySQL 用来补齐元数据，DW MySQL 用来读取数据库方言和版本
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)

            # 字段和指标分别使用不同 Qdrant collection，取值检索使用 ES index
            column_qdrant_repository = ColumnQdrantRepository(
                qdrant_client_manager.client
            )
            metric_qdrant_repository = MetricQdrantRepository(
                qdrant_client_manager.client
            )
            value_es_repository = ValueESRepository(es_client_manager.client)

            # 当前只需要传入原始问题，后续节点会逐步写回召回、过滤和额外上下文结果
            state = DataAgentState(query="统计华北地区的销售总额", retry_count=0, trace=[])
            context = DataAgentContext(
                column_qdrant_repository=column_qdrant_repository,
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=metric_qdrant_repository,
                value_es_repository=value_es_repository,
                meta_mysql_repository=meta_mysql_repository,
                dw_mysql_repository=dw_mysql_repository,
            )

            # stream_mode="custom" 会接收各节点通过 runtime.stream_writer 写出的进度信息
            async for chunk in graph.astream(
                input=state, context=context, stream_mode="custom"
            ):
                print(chunk)

        # 关闭显式创建的异步客户端，避免本地调试时连接资源悬挂
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    asyncio.run(test())
