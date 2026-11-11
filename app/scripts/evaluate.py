"""
问数 Agent 评测脚本

这个脚本负责回答一个很具体的问题：一次改造到底有没有让效果变好。
它提供两条互不干扰的评测通道：

1. 节点级评测：不跑整张图，直接调用三路召回节点，衡量候选集覆盖面
2. 端到端评测：跑完整 graph，衡量执行成功率 延迟 和 LLM 成本

两条通道分开的原因是一旦只看端到端分数，就无法判断问题出在召回错了
还是 SQL 生成错了，节点级指标负责定位，端到端指标负责验收

用法：
    uv run python -m app.scripts.evaluate
    uv run python -m app.scripts.evaluate --mode recall --limit 20
    uv run python -m app.scripts.evaluate --baseline evaluation/report_xxx.json

输出默认写入 evaluation/report_YYYYMMDD_HHMMSS.json
"""

import argparse
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
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

# 三条召回通道的评测配置：通道名 节点函数 state 字段 题目中的期望字段
RECALL_CHANNELS = (
    ("column", recall_column, "retrieved_column_infos", "expected_columns"),
    ("metric", recall_metric, "retrieved_metric_infos", "expected_metrics"),
    ("value", recall_value, "retrieved_value_infos", "expected_values"),
)

DEFAULT_QUESTIONS = Path("evaluation/questions.jsonl")
DEFAULT_OUTPUT_DIR = Path("evaluation")


@dataclass
class EvalQuestion:
    """评测集中的一道题，字段与 questions.jsonl 一行完全对应"""

    id: str
    query: str
    type: str
    expected_columns: list[str] = field(default_factory=list)
    expected_metrics: list[str] = field(default_factory=list)
    expected_values: list[str] = field(default_factory=list)
    expected_tables: list[str] = field(default_factory=list)
    expected_sql_keywords: list[str] = field(default_factory=list)
    notes: str = ""


class NodeRuntime:
    """节点函数只用到 context 和 stream_writer，这里提供最小可用的替身

    召回节点没有经过 LangGraph 调度，因此拿不到真正的 Runtime，
    手写一个只保留这两个属性的对象就能让节点原样执行
    """

    def __init__(self, context: DataAgentContext, events: list[dict]):
        self.context = context
        self.events = events
        self.stream_writer = events.append


class LLMTracker(UsageMetadataCallbackHandler):
    """在 token 统计之外补充调用次数，用于衡量单题 LLM 成本"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.calls += 1

    @property
    def total_tokens(self) -> int:
        """汇总所有模型返回的 total_tokens，服务未返回用量时结果为 0"""
        return sum(item.get("total_tokens", 0) for item in self.usage_metadata.values())


def load_questions(path: Path) -> list[EvalQuestion]:
    """逐行读取评测集，遇到字段缺失或未知字段时带上行号报错"""

    questions: list[EvalQuestion] = []
    seen_ids: set[str] = set()

    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                question = EvalQuestion(**json.loads(line))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"{path} 第 {line_no} 行格式错误：{exc}") from exc
            if question.id in seen_ids:
                raise ValueError(f"{path} 第 {line_no} 行 id 重复：{question.id}")
            seen_ids.add(question.id)
            questions.append(question)

    if not questions:
        raise ValueError(f"{path} 中没有读到任何题目")
    return questions


def coverage(predicted: list[str], expected: list[str]) -> float | None:
    """期望 id 中被召回的比例，没有期望值时返回 None 而不是 0"""

    if not expected:
        return None
    hit = len(set(expected) & set(predicted))
    return round(hit / len(expected), 4)


def contain_rate(text: str, tokens: list[str]) -> float | None:
    """检查 SQL 中出现了多少期望的表名或关键字，只用于粗筛明显错误"""

    if not tokens:
        return None
    upper_text = text.upper()
    hit = sum(token.upper() in upper_text for token in tokens)
    return round(hit / len(tokens), 4)


async def run_recall(question: EvalQuestion, context: DataAgentContext) -> dict:
    """单独执行关键词抽取和三路召回，不触发 SQL 生成与执行"""

    runtime = NodeRuntime(context, [])
    state = DataAgentState(query=question.query, retry_count=0, trace=[])
    state.update(await extract_keywords(state, runtime))

    channel_results: dict[str, dict] = {}
    for channel, node, state_key, expected_field in RECALL_CHANNELS:
        expected = getattr(question, expected_field)
        entry: dict[str, Any] = {
            "expected": len(expected),
            "predicted": 0,
            "coverage": None,
            "error": None,
        }
        try:
            # 节点返回的是 state 增量，直接合并回同一个 state 即可继续后续通道
            state.update(await node(state, runtime))
            predicted = [entity.id for entity in state[state_key]]
            entry["predicted"] = len(predicted)
            entry["coverage"] = coverage(predicted, expected)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        channel_results[channel] = entry

    return channel_results


async def run_e2e(question: EvalQuestion, context: DataAgentContext) -> dict:
    """跑完整问数链路，记录延迟 成本 执行结果和 SQL 粗筛指标"""

    tracker = LLMTracker()
    state = DataAgentState(query=question.query, retry_count=0, trace=[])
    final_state: dict = {}
    result_events = 0
    error = None
    started = time.perf_counter()

    try:
        # values 模式会在每个节点后返回整份 state，最后一份就包含最终 SQL
        async for mode, chunk in graph.astream(
            input=state,
            context=context,
            stream_mode=["custom", "values"],
            config={"callbacks": [tracker]},
        ):
            if mode == "values":
                final_state = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "result":
                result_events += 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    sql = (final_state.get("sql") or "").strip()

    return {
        "latency_ms": latency_ms,
        "success": error is None and bool(sql) and result_events > 0,
        "error": error,
        "llm_calls": tracker.calls,
        "total_tokens": tracker.total_tokens,
        "sql": sql,
        # 节点级轨迹由 graph 写入 state，报告里保留一份便于定位慢节点
        "trace": final_state.get("trace", []),
        "sql_keyword_coverage": contain_rate(sql, question.expected_sql_keywords),
        "table_hit_coverage": contain_rate(sql, question.expected_tables),
    }


def build_summary(records: list[dict]) -> dict:
    """把逐题明细聚合成可用于对比的一组数字"""

    summary: dict[str, Any] = {"questions": len(records), "recall": {}, "e2e": {}}

    for channel, _, _, _ in RECALL_CHANNELS:
        entries = [
            record["recall"][channel]
            for record in records
            if record.get("recall") and channel in record["recall"]
        ]
        coverages = [
            entry["coverage"] for entry in entries if entry["coverage"] is not None
        ]
        summary["recall"][f"{channel}_coverage"] = (
            round(mean(coverages), 4) if coverages else None
        )
        # 只有声明了期望值的题目才参与命中率，否则没有期望值的题会被当成未命中
        summary["recall"][f"{channel}_hit"] = (
            round(mean(1 if value > 0 else 0 for value in coverages), 4)
            if coverages
            else None
        )
        summary["recall"][f"{channel}_candidates"] = (
            round(mean([entry["predicted"] for entry in entries]), 2)
            if entries
            else None
        )
        summary["recall"][f"{channel}_errors"] = sum(
            1 for entry in entries if entry["error"]
        )

    e2e_records = [record["e2e"] for record in records if record.get("e2e")]
    if e2e_records:
        latencies = [record["latency_ms"] for record in e2e_records]
        summary["e2e"] = {
            "success_rate": round(
                mean(1 if record["success"] else 0 for record in e2e_records), 4
            ),
            "p50_latency_ms": percentile(latencies, 0.5),
            "p95_latency_ms": percentile(latencies, 0.95),
            "avg_llm_calls": round(
                mean(record["llm_calls"] for record in e2e_records), 2
            ),
            "avg_total_tokens": round(
                mean(record["total_tokens"] for record in e2e_records), 1
            ),
            "avg_sql_keyword_coverage": average_optional(
                record["sql_keyword_coverage"] for record in e2e_records
            ),
            "avg_table_hit_coverage": average_optional(
                record["table_hit_coverage"] for record in e2e_records
            ),
            "failed_ids": [record["id"] for record in records
                           if record.get("e2e") and not record["e2e"]["success"]],
        }

    return summary


def percentile(values: list[float], ratio: float) -> float | None:
    """最近秩法计算分位数，样本很少时也能给出一个可读的数字"""

    if not values:
        return None
    ordered = sorted(values)
    index = round(ratio * (len(ordered) - 1))
    return round(ordered[index], 1)


def average_optional(values) -> float | None:
    """忽略 None 求平均，某项指标整体缺失时返回 None"""

    collected = [value for value in values if value is not None]
    return round(mean(collected), 4) if collected else None


def git_commit() -> str:
    """记录本次报告对应的代码版本，保证数字可以回溯"""

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def flatten(prefix: str, value: Any, output: dict[str, float]) -> None:
    """把嵌套汇总字典压平成点号路径，便于和基线逐项对比"""

    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}.{key}" if prefix else key, item, output)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        output[prefix] = value


def build_context(
    meta_session, dw_session
) -> DataAgentContext:
    """按 API 依赖层的方式组装一次评测运行所需的外部依赖"""

    return DataAgentContext(
        column_qdrant_repository=ColumnQdrantRepository(qdrant_client_manager.client),
        embedding_client=embedding_client_manager.client,
        metric_qdrant_repository=MetricQdrantRepository(qdrant_client_manager.client),
        value_es_repository=ValueESRepository(es_client_manager.client),
        meta_mysql_repository=MetaMySQLRepository(meta_session),
        dw_mysql_repository=DWMySQLRepository(dw_session),
    )


async def evaluate(args: argparse.Namespace) -> dict:
    """初始化依赖并按题目逐条执行评测"""

    questions = load_questions(Path(args.questions))
    if args.limit:
        questions = questions[: args.limit]

    # 和构建脚本保持一致：所有客户端在进程内初始化一次，结束时统一关闭
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()

    records: list[dict] = []
    try:
        for index, question in enumerate(questions, start=1):
            print(f"[{index}/{len(questions)}] {question.id} {question.query}")
            # 每题使用独立 Session，和一次 HTTP 请求的生命周期保持一致，
            # 避免某道题的 SQL 校验失败污染后续题目的数据库会话
            async with (
                meta_mysql_client_manager.session_factory() as meta_session,
                dw_mysql_client_manager.session_factory() as dw_session,
            ):
                context = build_context(meta_session, dw_session)
                record: dict[str, Any] = {
                    "id": question.id,
                    "query": question.query,
                    "type": question.type,
                }
                if args.mode in ("recall", "all"):
                    record["recall"] = await run_recall(question, context)
                if args.mode in ("e2e", "all"):
                    record["e2e"] = await run_e2e(question, context)
            records.append(record)
    finally:
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()
        await qdrant_client_manager.close()
        await es_client_manager.close()

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "dataset": str(args.questions),
            "mode": args.mode,
            "limit": args.limit,
        },
        "summary": build_summary(records),
        "questions": records,
    }


def print_summary(summary: dict) -> None:
    """把汇总指标按行打印，便于直接贴进改造记录"""

    print("\n=== 汇总指标 ===")
    for channel, _, _, _ in RECALL_CHANNELS:
        print(
            f"{channel:>6} 覆盖率={summary['recall'][f'{channel}_coverage']}"
            f" 命中率={summary['recall'][f'{channel}_hit']}"
            f" 候选数={summary['recall'][f'{channel}_candidates']}"
            f" 错误数={summary['recall'][f'{channel}_errors']}"
        )
    if summary["e2e"]:
        for key, value in summary["e2e"].items():
            print(f"{key} = {value}")


def print_delta(summary: dict, baseline: dict) -> None:
    """与基线报告逐项比较，只打印双方都有的数值指标"""

    current: dict[str, float] = {}
    previous: dict[str, float] = {}
    flatten("", summary, current)
    flatten("", baseline, previous)

    print("\n=== 对比基线 ===")
    for key, value in current.items():
        if key not in previous:
            continue
        delta = round(value - previous[key], 4)
        print(f"{key}: {previous[key]} -> {value} ({delta:+})")


def parse_args() -> argparse.Namespace:
    """命令行参数只保留评测真正需要的开关"""

    parser = argparse.ArgumentParser(description="问数 Agent 离线评测")
    parser.add_argument(
        "--questions", default=str(DEFAULT_QUESTIONS), help="评测集 jsonl 路径"
    )
    parser.add_argument(
        "--output", default="", help="报告输出路径，默认按时间戳生成"
    )
    parser.add_argument(
        "--mode",
        choices=("recall", "e2e", "all"),
        default="all",
        help="recall 只跑节点级召回，e2e 只跑完整链路",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题，0 表示全部")
    parser.add_argument("--baseline", default="", help="基线报告路径，用于打印对比")
    return parser.parse_args()


async def main() -> None:
    """执行评测 写出报告 并按需与基线对比"""

    args = parse_args()
    report = await evaluate(args)

    output = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_DIR
        / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_summary(report["summary"])
    print(f"\n报告已写入：{output}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        print_delta(report["summary"], baseline.get("summary", {}))


if __name__ == "__main__":
    asyncio.run(main())
