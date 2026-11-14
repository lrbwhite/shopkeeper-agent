"""
字段召回节点

负责根据关键词从字段向量知识库中召回候选字段
它解决的是“用户问题可能对应哪些数据库字段”的问题
本章的主线是：读取上游字段词表 -> Embedding -> Qdrant 相似度检索 -> ColumnInfo 去重
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger
from app.entities.column_info import ColumnInfo


async def recall_column(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回和用户问题语义相关的字段元数据"""

    writer = runtime.stream_writer
    step = "召回字段信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 字段召回词表由上游 suggest_keywords 统一产出；排序保证多次运行结果顺序稳定
        keywords = sorted(set(state["keywords"]) | set(state["column_keywords"]))
        # context 保存外部运行时工具：向量仓储和 Embedding 客户端
        column_qdrant_repository = runtime.context["column_qdrant_repository"]
        embedding_client = runtime.context["embedding_client"]

        # 关键词一次性批量向量化，把 N 次串行 embedding 调用压缩成 1 次
        embeddings = await embedding_client.aembed_documents(keywords)
        # 仓储内部按配置的并发上限并行检索，返回顺序与关键词顺序一致
        result_groups = await column_qdrant_repository.search_many(embeddings)

        # 用字段 id 做唯一键，因为多个关键词、同一字段的多个向量点都可能命中同一个字段
        column_info_map: dict[str, ColumnInfo] = {}
        for current_column_infos in result_groups:
            for column_info in current_column_infos:
                # setdefault 保留首个命中，结果顺序由关键词顺序决定
                column_info_map.setdefault(column_info.id, column_info)

        # 写回 state 的是去重后的 ColumnInfo 列表，不暴露 Qdrant 原始 point 结构
        retrieved_column_infos: list[ColumnInfo] = list(column_info_map.values())

        writer({"type": "progress", "step": step, "status": "success"})
        return {"retrieved_column_infos": retrieved_column_infos}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
