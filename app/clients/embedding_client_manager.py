"""
Embedding 客户端管理器

负责按配置初始化 Embedding 服务客户端，并为字段、指标和用户问题的向量化
提供统一访问入口

云端 Embedding 走 OpenAI 兼容协议（当前使用阿里云百炼 text-embedding-v4），
对外仍然暴露 LangChain 的 Embeddings 接口，调用方无感知
"""

import asyncio
from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.conf.app_config import EmbeddingConfig, app_config


class EmbeddingClientManager:
    """管理 Embedding 服务客户端的初始化与复用"""

    def __init__(self, config: EmbeddingConfig):
        self.client: Optional[Embeddings] = None
        self.config = config

    def init(self):
        """显式初始化客户端，避免模块导入时立即建立外部连接"""
        # check_embedding_ctx_length=False 让请求直接携带原始文本，
        # 避免本地 tiktoken 分词结果与非 OpenAI 服务不匹配的问题
        self.client = OpenAIEmbeddings(
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            check_embedding_ctx_length=False,
            max_retries=3,
        )


# 模块级单例，供整个项目复用同一套 Embedding 客户端管理器
embedding_client_manager = EmbeddingClientManager(app_config.embedding)


if __name__ == "__main__":
    embedding_client_manager.init()
    client = embedding_client_manager.client

    async def test():
        """执行一次最小化向量化调用，验证服务是否可用"""
        text = "What is deep learning?"
        query_result = await client.aembed_query(text)
        print(query_result[:3])

    asyncio.run(test())
