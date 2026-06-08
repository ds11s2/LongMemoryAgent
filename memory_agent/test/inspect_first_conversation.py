#!/usr/bin/env python3
"""
导入第一条 conversation 并导出检查报告到 Markdown 文件。

输出：test/first_conversation_inspection.md
内容：每个 session 的原始对话原文、提取出的记忆单元、每条记忆的重要性分数
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

EVAL_SET_PATH = os.path.join(PROJECT_ROOT, "eval_kit", "eval_kit", "eval_set.json")
OUTPUT_MD = os.path.join(os.path.dirname(__file__), "first_conversation_inspection.md")

# 与 writer.py 中一致的日期工具
_DATE_FORMAT = "%I:%M %p on %d %B, %Y"


def _parse_date_time(date_str: str) -> float:
    return datetime.strptime(date_str, _DATE_FORMAT).timestamp()


def _format_session_date(date_str: str) -> str:
    dt = datetime.strptime(date_str, _DATE_FORMAT)
    day = dt.day
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return dt.strftime(f"%B {day}{suffix}, %Y")


def main():
    from memory_agent.agent.controller import MemoryAgent

    # 1. 加载第一条 conversation
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        eval_set = json.load(f)

    first = eval_set[0]
    sample_id = first["sample_id"]
    conversation = first["conversation"]
    sessions = conversation["sessions"]
    num_sessions = len(sessions)

    print(f"第一条 conversation: {sample_id}, 共 {num_sessions} 个 session")

    # 2. 构建 session 时间戳 → session 索引的映射
    ts_to_session = {}
    session_info = {}  # idx -> {date_str, turns_text}
    for idx, sess in enumerate(sessions):
        ts = _parse_date_time(sess["date_time"])
        ts_to_session[ts] = idx
        turns_text = "\n".join(
            f"{turn['speaker']}: {turn['text']}" for turn in sess["turns"]
        )
        session_info[idx] = {
            "date_str": sess["date_time"],
            "formatted_date": _format_session_date(sess["date_time"]),
            "turns_text": turns_text,
            "turn_count": len(sess["turns"]),
        }

    # 3. 调用 ingest 导入
    print("正在导入 conversation ...")
    agent = MemoryAgent()
    agent.ingest(conversation)
    print("导入完成。")

    # 4. 从 store 中取出所有 observation 记忆，按 creation_timestamp 分组
    memories = agent.store.get_all()
    grouped = defaultdict(list)  # session_idx -> [MemoryUnit, ...]
    for mem in memories:
        if mem.type == "observation":
            ts = mem.creation_timestamp
            if ts in ts_to_session:
                grouped[ts_to_session[ts]].append(mem)

    # 5. 生成 Markdown
    lines = []
    lines.append(f"# 第一条 Conversation 检查报告")
    lines.append(f"")
    lines.append(f"- **Sample ID**: `{sample_id}`")
    lines.append(f"- **Session 数量**: {num_sessions}")
    lines.append(f"- **提取的记忆单元总数** (observation): {len(memories)}")
    lines.append(f"")

    # ── 人物总结（放在最前面）──
    lines.append(f"## 人物总结")
    lines.append(f"")
    if agent.persona_summaries:
        lines.append(agent.persona_summaries)
    else:
        lines.append(f"> 未生成人物总结。")
    lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    for idx in range(num_sessions):
        info = session_info[idx]
        session_memories = grouped.get(idx, [])

        lines.append(f"## Session {idx + 1} / {num_sessions}")
        lines.append(f"")
        lines.append(f"- **日期**: {info['formatted_date']} ({info['date_str']})")
        lines.append(f"- **对话轮数**: {info['turn_count']}")
        lines.append(f"- **提取记忆条数**: {len(session_memories)}")
        lines.append(f"")

        # ── 原始对话原文 ──
        lines.append(f"### 原始对话")
        lines.append(f"")
        lines.append(f"```text")
        lines.append(info["turns_text"])
        lines.append(f"```")
        lines.append(f"")

        # ── 提取出的记忆单元及重要性分数 ──
        lines.append(f"### 提取的记忆单元")
        lines.append(f"")
        if session_memories:
            lines.append(f"| # | 重要性 | 记忆内容 |")
            lines.append(f"|---|--------|----------|")
            for i, mem in enumerate(session_memories, 1):
                lines.append(f"| {i} | {int(mem.importance_score)} | {mem.text_description} |")
        else:
            lines.append(f"> 该 session 未提取到任何记忆单元。")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n报告已生成: {OUTPUT_MD}")


if __name__ == "__main__":
    main()
