"""
字段取值 ES 仓储

把字段真实取值组织成 Elasticsearch 全文索引，并提供索引创建 批量写入和关键词检索能力

Service 层负责决定哪些字段需要同步
Repository 只关心索引是否存在 ValueInfo 如何写进 ES 以及如何按关键词召回
"""

from dataclasses import asdict

from elasticsearch import AsyncElasticsearch

from app.entities.value_info import ValueInfo


class ValueESRepository:
    """负责字段取值全文索引的创建 写入和基础检索"""

    index_name = "value_index"
    # value 字段使用 IK 分词，这样地区 会员等级 品类等中文值才能按全文方式检索
    index_mappings = {
        "dynamic": False,
        "properties": {
            "id": {"type": "keyword"},
            "value": {
                "type": "text",
                "analyzer": "ik_max_word",
                "search_analyzer": "ik_max_word",
            },
            "column_id": {"type": "keyword"},
        },
    }

    def __init__(self, client: AsyncElasticsearch):
        self.client = client

    async def ensure_index(self):
        """确保字段取值索引已经创建好"""
        if not await self.client.indices.exists(index=self.index_name):
            await self.client.indices.create(
                index=self.index_name, mappings=self.index_mappings
            )

    async def _list_doc_ids(self) -> list[str]:
        """滚动读取索引里的全部文档 id"""
        resp = await self.client.search(
            index=self.index_name,
            query={"match_all": {}},
            size=100,
            sort=["_doc"],
            scroll="5m",
        )
        scroll_id = resp["_scroll_id"]
        doc_ids = [hit["_id"] for hit in resp["hits"]["hits"]]

        while resp["hits"]["hits"]:
            resp = await self.client.scroll(scroll_id=scroll_id, scroll="5m")
            doc_ids.extend(hit["_id"] for hit in resp["hits"]["hits"])

        await self.client.clear_scroll(scroll_id=scroll_id)
        return doc_ids

    async def reconcile(self, keep_ids: set[str]) -> int:
        """删除不属于本次构建结果的文档，返回值被删除的文档数"""
        if not await self.client.indices.exists(index=self.index_name):
            return 0

        stale_ids = [doc_id for doc_id in await self._list_doc_ids() if doc_id not in keep_ids]
        if not stale_ids:
            return 0

        operations = [
            {"delete": {"_index": self.index_name, "_id": doc_id}}
            for doc_id in stale_ids
        ]
        for i in range(0, len(operations), 20):
            await self.client.bulk(operations=operations[i : i + 20])
        await self.client.indices.refresh(index=self.index_name)
        return len(stale_ids)

    async def index(self, value_infos: list[ValueInfo], batch_size=20):
        """分批写入字段取值，避免一次 bulk 过大"""
        if not value_infos:
            return

        for i in range(0, len(value_infos), batch_size):
            batch_value_infos = value_infos[i : i + batch_size]
            batch_operations = []
            for value_info in batch_value_infos:
                # 用 ValueInfo.id 作为文档 id，这样重复构建时会覆盖同一条值记录
                batch_operations.append(
                    {"index": {"_index": self.index_name, "_id": value_info.id}}
                )
                batch_operations.append(asdict(value_info))
            await self.client.bulk(operations=batch_operations)

    async def search(
        self, keyword: str, score_threshold: float = 0.6, limit: int = 20
    ) -> list[ValueInfo]:
        """按关键词全文检索字段取值，并还原为 ValueInfo 实体"""

        resp = await self.client.search(
            index=self.index_name,
            # value 字段启用了 IK 分词，match 查询可以处理中文短语和枚举值匹配
            query={"match": {"value": keyword}},
            size=limit,
            # 过滤掉相关度过低的命中，避免把明显无关的取值带入后续上下文
            min_score=score_threshold,
        )
        # ES 文档 _source 中保存的是 ValueInfo 的字段结构，业务层继续使用实体对象
        return [ValueInfo(**hit["_source"]) for hit in resp["hits"]["hits"]]
