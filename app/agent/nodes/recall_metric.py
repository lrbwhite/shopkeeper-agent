"""
指标召回节点

负责根据用户问题从指标向量知识库中召回候选指标
它帮助 Agent 把“销售额 转化率 客单价”等业务表达映射到已定义指标
实现路径和字段召回类似：读取上游指标词表 -> Embedding -> Qdrant 相似度检索 -> MetricInfo 去重
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger
from app.entities.metric_info import MetricInfo


async def recall_metric(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回和用户问题语义相关的业务指标"""

    writer = runtime.stream_writer
    step = "召回指标信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 指标召回词表由上游 suggest_keywords 统一产出；排序保证多次运行结果顺序稳定
        keywords = sorted(set(state["keywords"]) | set(state["metric_keywords"]))
        # 指标召回使用向量检索，需要 Embedding 客户端和指标 Qdrant 仓储配合
        embedding_client = runtime.context["embedding_client"]
        metric_qdrant_repository = runtime.context["metric_qdrant_repository"]

        # 关键词一次性批量向量化，把 N 次串行 embedding 调用压缩成 1 次
        embeddings = await embedding_client.aembed_documents(keywords)
        # 仓储内部按配置的并发上限并行检索，返回顺序与关键词顺序一致
        result_groups = await metric_qdrant_repository.search_many(embeddings)

        # 用指标 id 做唯一键，避免多个关键词命中同一个指标时重复写入 state
        metric_info_map: dict[str, MetricInfo] = {}
        for current_metric_infos in result_groups:
            for metric_info in current_metric_infos:
                metric_info_map.setdefault(metric_info.id, metric_info)

        # 写回 state 的是业务实体列表，后续过滤节点不需要关心 Qdrant 原始 point 结构
        retrieved_metric_infos: list[MetricInfo] = list(metric_info_map.values())
        logger.info(f"检索到指标信息：{list(metric_info_map.keys())}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"retrieved_metric_infos": retrieved_metric_infos}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
