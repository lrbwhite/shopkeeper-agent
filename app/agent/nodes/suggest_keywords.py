"""
关键词扩展节点

一次结构化输出同时产出字段 指标 取值三路召回词表
后续三路召回节点只读 state 中各自的词表，不再分别调用大模型
这样单次问数的关键词扩展调用从 3 次降到 1 次，三路理解也保持一致
"""

from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 单路词表上限，避免模型发散后同义词挤占召回名额
MAX_KEYWORDS_PER_CHANNEL = 6


class KeywordSuggestions(BaseModel):
    """一次调用同时产出的三路召回词表"""

    column_keywords: list[str] = Field(default_factory=list, description="字段语义词列表")
    metric_keywords: list[str] = Field(default_factory=list, description="指标概念词列表")
    value_keywords: list[str] = Field(default_factory=list, description="字段取值候选词列表")


def clean_keywords(words: list[str], query: str) -> list[str]:
    """去掉空白与重复，丢弃与原始问题完全相同的词，并限制单路词表长度"""

    cleaned: list[str] = []
    for word in words:
        word = (word or "").strip()
        # 原始问题已经作为兜底检索词存在于 keywords 中，这里不重复收录
        if not word or word == query or word in cleaned:
            continue
        cleaned.append(word)
        if len(cleaned) >= MAX_KEYWORDS_PER_CHANNEL:
            break
    return cleaned


async def suggest_keywords(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """一次调用同时扩展出字段 指标 取值三路检索词"""

    step = "扩展关键词"
    writer = runtime.stream_writer
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]

        prompt = PromptTemplate(
            template=load_prompt("suggest_keywords"),
            input_variables=["query"],
        )
        # schema 通过 json_mode 的响应格式约束传递，prompt 中不需要写 JSON 示例，
        # 避免花括号被当成模板变量；同时绕开工具调用 schema 在部分网关上的语法编译失败
        chain = prompt | llm.with_structured_output(
            KeywordSuggestions, method="json_mode"
        )

        result: KeywordSuggestions = await chain.ainvoke({"query": query})

        column_keywords = clean_keywords(result.column_keywords, query)
        metric_keywords = clean_keywords(result.metric_keywords, query)
        value_keywords = clean_keywords(result.value_keywords, query)

        logger.info(f"扩展字段词: {column_keywords}")
        logger.info(f"扩展指标词: {metric_keywords}")
        logger.info(f"扩展取值词: {value_keywords}")

        writer({"type": "progress", "step": step, "status": "success"})
        return {
            "column_keywords": column_keywords,
            "metric_keywords": metric_keywords,
            "value_keywords": value_keywords,
        }
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
