"""
元数据库 MySQL 仓储

这一层对应文档里的 Meta Repository，负责接收业务实体并落到 Meta MySQL
Repository 自身只关心“如何写入”，而“哪些写操作要放在同一笔事务里”，由 Service 层统一决定

表 字段 指标和字段指标关系都会先以业务实体流转，再在这里统一转成 ORM 模型
问数链路运行时也会从这里读取元数据，用来把召回到的 id 补齐成完整实体
"""

from dataclasses import asdict

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.entities.column_info import ColumnInfo
from app.entities.column_metric import ColumnMetric
from app.entities.metric_info import MetricInfo
from app.entities.table_info import TableInfo
from app.models.column_info import ColumnInfoMySQL
from app.models.column_metric import ColumnMetricMySQL
from app.models.metric_info import MetricInfoMySQL
from app.models.table_info import TableInfoMySQL
from app.repositories.mysql.meta.mappers.column_info_mapper import ColumnInfoMapper
from app.repositories.mysql.meta.mappers.table_info_mapper import TableInfoMapper


class MetaMySQLRepository:
    """负责把元数据业务实体持久化到 Meta MySQL"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_table_infos(self, table_infos: list[TableInfo]):
        """批量写入表元数据：重复构建时按主键覆盖，保证幂等"""
        if not table_infos:
            return

        stmt = mysql_insert(TableInfoMySQL).values(
            [asdict(table_info) for table_info in table_infos]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            role=stmt.inserted.role,
            description=stmt.inserted.description,
        )
        await self.session.execute(stmt)

    async def save_column_infos(self, column_infos: list[ColumnInfo]):
        """批量写入字段元数据：重复构建时按主键覆盖，保证幂等"""
        if not column_infos:
            return

        stmt = mysql_insert(ColumnInfoMySQL).values(
            [asdict(column_info) for column_info in column_infos]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            type=stmt.inserted.type,
            role=stmt.inserted.role,
            examples=stmt.inserted.examples,
            description=stmt.inserted.description,
            alias=stmt.inserted.alias,
            table_id=stmt.inserted.table_id,
        )
        await self.session.execute(stmt)

    async def save_metric_infos(self, metric_infos: list[MetricInfo]):
        """批量写入指标元数据：重复构建时按主键覆盖，保证幂等"""
        if not metric_infos:
            return

        stmt = mysql_insert(MetricInfoMySQL).values(
            [asdict(metric_info) for metric_info in metric_infos]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            description=stmt.inserted.description,
            relevant_columns=stmt.inserted.relevant_columns,
            alias=stmt.inserted.alias,
        )
        await self.session.execute(stmt)

    async def save_column_metrics(self, column_metrics: list[ColumnMetric]):
        """批量写入字段与指标的关联关系，重复关系直接跳过"""
        if not column_metrics:
            return

        stmt = mysql_insert(ColumnMetricMySQL).values(
            [asdict(column_metric) for column_metric in column_metrics]
        )
        # 联合主键已经存在时不需要更新任何列，这里用无实际变更的赋值避免报错
        stmt = stmt.on_duplicate_key_update(column_id=stmt.inserted.column_id)
        await self.session.execute(stmt)

    async def delete_table_infos_not_in(self, table_ids: list[str]) -> int:
        """删除不在本次构建结果里的表元数据，返回删除行数"""
        result = await self.session.execute(
            delete(TableInfoMySQL).where(TableInfoMySQL.id.not_in(table_ids))
        )
        return result.rowcount or 0

    async def delete_column_infos_not_in(self, column_ids: list[str]) -> int:
        """删除不在本次构建结果里的字段元数据，返回删除行数"""
        result = await self.session.execute(
            delete(ColumnInfoMySQL).where(ColumnInfoMySQL.id.not_in(column_ids))
        )
        return result.rowcount or 0

    async def delete_metric_infos_not_in(self, metric_ids: list[str]) -> int:
        """删除不在本次构建结果里的指标元数据，返回删除行数"""
        result = await self.session.execute(
            delete(MetricInfoMySQL).where(MetricInfoMySQL.id.not_in(metric_ids))
        )
        return result.rowcount or 0

    async def delete_column_metrics_not_in(
        self, column_ids: list[str], metric_ids: list[str]
    ) -> int:
        """删除引用已移除字段或指标的关联关系，返回删除行数"""
        conditions = []
        if column_ids:
            conditions.append(ColumnMetricMySQL.column_id.not_in(column_ids))
        if metric_ids:
            conditions.append(ColumnMetricMySQL.metric_id.not_in(metric_ids))
        if not conditions:
            return 0

        result = await self.session.execute(
            delete(ColumnMetricMySQL).where(or_(*conditions))
        )
        return result.rowcount or 0

    async def get_column_info_by_id(self, id: str) -> ColumnInfo | None:
        """按字段 id 查询字段元数据，供召回信息合并阶段补齐字段上下文"""

        column_info: ColumnInfoMySQL | None = await self.session.get(
            ColumnInfoMySQL, id
        )
        if column_info:
            return ColumnInfoMapper.to_entity(column_info)
        else:
            return None

    async def get_table_info_by_id(self, id: str) -> TableInfo | None:
        """按表 id 查询表元数据，最终组装成提示词里的表结构信息"""

        table_info: TableInfoMySQL | None = await self.session.get(TableInfoMySQL, id)
        if table_info:
            return TableInfoMapper.to_entity(table_info)
        else:
            return None

    async def get_key_columns_by_table_id(self, table_id: str) -> list[ColumnInfo]:
        """查询指定表的主外键字段，避免 Join 关键字段被向量召回漏掉"""

        # 主外键字段用于后续生成 join 条件，不能完全依赖向量召回命中
        # 这里走 ORM 查询而不是裸 SQL：column_info 的 examples / alias 是 JSON 列，
        # 只有带上 ORM 列类型信息，SQLAlchemy 才会把 JSON 字符串反序列化成 list，
        # 和 get_column_info_by_id() 保持一致的返回值形态
        stmt = select(ColumnInfoMySQL).where(
            ColumnInfoMySQL.table_id == table_id,
            ColumnInfoMySQL.role.in_(("primary_key", "foreign_key")),
        )
        result = await self.session.scalars(stmt)
        return [ColumnInfoMapper.to_entity(model) for model in result.all()]
