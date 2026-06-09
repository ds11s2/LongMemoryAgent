#!/usr/bin/env python3
"""
单 Conversation 详细检索测试脚本

对第一个 conversation 的每个问题逐一回答、评测，并将每个问题的
检索到的记忆单元详情写入 MD 报告。

用法：
  python test/retriever_test.py
"""

import json
import os
import re
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

EVAL_SET_PATH = os.path.join(PROJECT_ROOT, "eval_kit", "eval_kit", "eval_set.json")
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SAVED_DB_ROOT = os.path.join(TEST_DIR, "saved_chroma_db")
PERSONA_FILE = os.path.join(TEST_DIR, "persona_summaries.json")
OUTPUT_MD = os.path.join(TEST_DIR, "retriever_test_report.md")

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# ═══════════════════════════════════════════════════════
#  Judge 逻辑（内嵌，避免依赖 eval_kit 子进程）
# ═══════════════════════════════════════════════════════
JUDGE_SYSTEM = (
    "You are a strict but fair grading assistant. You grade whether a predicted answer "
    "to a question captures the same information as the reference answer. "
    "You output a single JSON object, nothing else."
)

JUDGE_PROMPT = """Grade the prediction against the reference.

Question: {question}
Reference answer: {reference}
Predicted answer: {prediction}

Apply this rubric:
- CORRECT: The prediction conveys the same key information as the reference. Paraphrasing, different wording, and additional relevant detail are fine. For dates/numbers, the values must match (format can differ).
- PARTIAL: The prediction is on the right topic and partially overlaps with the reference (e.g., correct category but missing specific item; correct year but wrong month), but is not fully accurate.
- WRONG: The prediction is missing, contradictory, hallucinated, or on the wrong topic.

Special cases:
- If the reference is a short entity/date and the prediction is a long sentence that clearly contains the correct information, label CORRECT.
- "I don't know" / empty prediction is WRONG unless the reference also indicates unanswerable.

Respond with JSON only, no other text:
{{"reasoning": "<one short sentence>", "label": "CORRECT" | "PARTIAL" | "WRONG"}}"""

LABEL_SCORE = {"CORRECT": 1.0, "PARTIAL": 0.5, "WRONG": 0.0}


def parse_judge_output(text: str) -> dict:
    if not text:
        return {"label": "WRONG", "reasoning": "empty judge output"}
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {"label": "WRONG", "reasoning": f"unparseable: {text[:100]}"}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"label": "WRONG", "reasoning": f"unparseable: {text[:100]}"}
    label = str(obj.get("label", "")).upper().strip()
    if label not in LABEL_SCORE:
        for key in LABEL_SCORE:
            if key in label:
                label = key
                break
        else:
            label = "WRONG"
    return {"label": label, "reasoning": str(obj.get("reasoning", ""))[:300]}


# ═══════════════════════════════════════════════════════
#  DB 加载
# ═══════════════════════════════════════════════════════
def _load_store_from_disk(persist_dir: str):
    import chromadb
    from memory_agent.memory.store import MemoryStore, COLLECTION_NAME

    store = MemoryStore.__new__(MemoryStore)
    store.client = chromadb.PersistentClient(path=persist_dir)
    store.collection = store.client.get_collection(COLLECTION_NAME)
    store._next_id = store.collection.count()
    return store


# ═══════════════════════════════════════════════════════
#  Answer Prompt（与 run_test.py 一致）
# ═══════════════════════════════════════════════════════
ANSWER_PROMPT = """
You are a fact extractor. Your task is to find the exact answer from the Memory Logs.
[Persona Profiles]
{persona_context}

[Memory Logs]
{memory_context}

Question: {question}

RULES:
1. ONLY use the information directly written in the memories.
2. If the question asks for a specific fact (e.g., date, name, item), find the sentence that exactly answers it and copy that fact.
3. Output ONLY the answer, no extra words.

Answer:"""


FILTER_PROMPT = """
Your task is to keep only the memories that help answer the question. Delete all others.

Memories:
{memory_context}

Question: {question}

Rules:
- If a memory contains information directly related to the question, keep it.
- If it does not, delete it.
- Output only the kept memories, exactly as they appear. Do not add anything. Do not explain.
"""


# ═══════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════
def main():
    from memory_agent.memory.writer import MemoryWriter
    from memory_agent.memory.retriever import MemoryRetriever
    from llm_client import LLMClient

    # ── 加载数据 ──
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        eval_set = json.load(f)

    with open(PERSONA_FILE, encoding="utf-8") as f:
        all_personas = json.load(f)

    first_sample = eval_set[0]
    sample_id = first_sample["sample_id"]
    qa_list = first_sample["qa_list"]
    conversation = first_sample["conversation"]

    print(f"目标 conversation: {sample_id}")
    print(f"对话双方: {conversation['speaker_a']} & {conversation['speaker_b']}")
    print(f"问题总数: {len(qa_list)}")

    conv_db_dir = os.path.join(SAVED_DB_ROOT, sample_id)
    if not os.path.exists(conv_db_dir):
        print(f"错误: 未找到数据库 {conv_db_dir}，请先运行 run_test.py -ingest")
        sys.exit(1)

    # ── 构建组件 ──
    writer = MemoryWriter()
    llm = LLMClient()  # 本地 vllm（ANSWER）

    # Judge 用 deepseek-v4-flash
    os.environ["LLM_MODEL"] = "deepseek-v4-flash"
    os.environ["LLM_BASE_URL"] = "https://api.deepseek.com/v1"
    os.environ["LLM_API_KEY"] = "sk-97a50281d2b442ab84408c6c9d73be41"
    judge_llm = LLMClient(temperature=0.0)

    store = _load_store_from_disk(conv_db_dir)
    retriever = MemoryRetriever(
        store=store,
        embed_model=writer.embed_model,
        writer=writer,
    )
    persona = all_personas.get(sample_id, "")

    total_memories = store.collection.count()
    print(f"记忆总数: {total_memories}")

    # ── 逐题回答 + 评测 + 收集检索详情 ──
    results = []
    category_scores = {}

    for qi, qa in enumerate(qa_list):
        qa_id = qa["qa_id"]
        question = qa["question"]
        reference = qa["answer"]
        category = qa["category_name"]

        print(f"\n[{qi+1}/{len(qa_list)}] {qa_id}: {question}")

        # ── 检索 ──
        t0 = time.time()
        query_embedding = writer.embed_text(question)
        retrieved = retriever.retrieve(question, query_embedding, top_k=25)
        retrieve_time = time.time() - t0

        # ── 回答 ──
        if not retrieved:
            prediction = "unknown"
            answer_time = 0
        else:
            # ── 格式化：仅去掉开头的 [Conversation Date: xxx] ──
            context_lines = []
            for mem, _ in retrieved:
                text = mem.text_description
                cleaned = re.sub(r'^\[Conversation Date: [^\]]+\]\s*', '', text)
                context_lines.append(f"- {cleaned}")
            context = "\n".join(context_lines)

            # ── 第一阶段：精简记忆单元 ──
            t1 = time.time()
            filter_prompt = FILTER_PROMPT.format(
                memory_context=context,
                question=question,
            )
            filtered_context = llm.generate(filter_prompt, max_tokens=512).strip()

            # ── 第二阶段：用精简后的记忆回答问题 ──
            prompt = ANSWER_PROMPT.format(
                memory_context=filtered_context,
                question=question,
                persona_context=persona,
            )
            prediction = llm.generate(prompt, max_tokens=64).strip()
            answer_time = time.time() - t1

        total_time = retrieve_time + answer_time

        # ── Judge ──
        judge_prompt = JUDGE_PROMPT.format(
            question=question,
            reference=str(reference),
            prediction=str(prediction).strip(),
        )
        judge_raw = judge_llm.generate(judge_prompt, max_tokens=256, system=JUDGE_SYSTEM, temperature=0.0)
        judge_parsed = parse_judge_output(judge_raw)
        judge_label = judge_parsed["label"]
        judge_reasoning = judge_parsed["reasoning"]
        judge_score = LABEL_SCORE[judge_label]

        # ── 记录 ──
        result = {
            "qa_id": qa_id,
            "question": question,
            "reference": str(reference),
            "prediction": str(prediction).strip(),
            "category": category,
            "retrieve_time": retrieve_time,
            "answer_time": answer_time,
            "total_time": total_time,
            "judge_label": judge_label,
            "judge_reasoning": judge_reasoning,
            "judge_score": judge_score,
            "judge_raw": judge_raw,
            "retrieved": [(mem, score) for mem, score in retrieved],
        }
        results.append(result)

        # 统计
        if category not in category_scores:
            category_scores[category] = {"n": 0, "score": 0}
        category_scores[category]["n"] += 1
        category_scores[category]["score"] += judge_score

        print(f"  -> prediction: {prediction}")
        print(f"  -> judge: {judge_label} ({judge_reasoning})")
        print(f"  -> retrieved: {len(retrieved)} memories, "
              f"检索 {retrieve_time:.2f}s + 生成 {answer_time:.2f}s")

    # ═══════════════════════════════════════════════════
    #  写入 MD 报告
    # ═══════════════════════════════════════════════════
    lines = []
    lines.append(f"# Retriever Test Report — {sample_id}")
    lines.append("")
    lines.append(f"- **生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **对话双方**: {conversation['speaker_a']} & {conversation['speaker_b']}")
    lines.append(f"- **记忆总数**: {total_memories}")
    lines.append(f"- **问题总数**: {len(qa_list)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 总体评测指标")
    lines.append("")

    overall_n = len(results)
    overall_score = sum(r["judge_score"] for r in results) / overall_n if overall_n else 0
    correct = sum(1 for r in results if r["judge_label"] == "CORRECT")
    partial = sum(1 for r in results if r["judge_label"] == "PARTIAL")
    wrong = sum(1 for r in results if r["judge_label"] == "WRONG")
    avg_retrieve = sum(r["retrieve_time"] for r in results) / overall_n
    avg_answer = sum(r["answer_time"] for r in results) / overall_n
    avg_total = sum(r["total_time"] for r in results) / overall_n

    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|----|")
    lines.append(f"| 得分 (Score) | {overall_score:.3f} |")
    lines.append(f"| CORRECT | {correct} |")
    lines.append(f"| PARTIAL | {partial} |")
    lines.append(f"| WRONG | {wrong} |")
    lines.append(f"| 平均检索耗时 | {avg_retrieve:.2f}s |")
    lines.append(f"| 平均回答耗时 | {avg_answer:.2f}s |")
    lines.append(f"| 平均总耗时 | {avg_total:.2f}s |")
    lines.append("")

    lines.append("## 分类别得分")
    lines.append("")
    lines.append(f"| 类别 | 题数 | 得分 | 正确 | 部分 | 错误 |")
    lines.append(f"|------|------|------|------|------|------|")
    for cat_name in sorted(category_scores.keys()):
        d = category_scores[cat_name]
        c_correct = sum(1 for r in results
                        if r["category"] == cat_name and r["judge_label"] == "CORRECT")
        c_partial = sum(1 for r in results
                        if r["category"] == cat_name and r["judge_label"] == "PARTIAL")
        c_wrong = sum(1 for r in results
                       if r["category"] == cat_name and r["judge_label"] == "WRONG")
        cat_score = d["score"] / d["n"]
        lines.append(f"| {cat_name} | {d['n']} | {cat_score:.3f} | {c_correct} | {c_partial} | {c_wrong} |")

    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 人物总结 (Persona Summary)")
    lines.append("")
    lines.append(persona.strip().replace("\n", "\n\n"))
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 逐题详情")
    lines.append("")

    for ri, r in enumerate(results):
        lines.append(f"### {ri+1}. {r['qa_id']}")
        lines.append("")
        lines.append(f"- **问题**: {r['question']}")
        lines.append(f"- **参考答案**: {r['reference']}")
        lines.append(f"- **AI 预测**: {r['prediction']}")
        lines.append(f"- **类别**: {r['category']}")
        lines.append(f"- **检索耗时**: {r['retrieve_time']:.2f}s")
        lines.append(f"- **回答耗时**: {r['answer_time']:.2f}s")
        lines.append(f"- **总耗时**: {r['total_time']:.2f}s")
        lines.append("")
        lines.append("#### 评测结果")
        lines.append("")
        judge_icon = {"CORRECT": "✅", "PARTIAL": "⚠️", "WRONG": "❌"}.get(r["judge_label"], "")
        lines.append(f"- **{judge_icon} 判定**: {r['judge_label']} (score={r['judge_score']})")
        lines.append(f"- **理由**: {r['judge_reasoning']}")
        lines.append("")
        lines.append("#### 命中词条（按综合分数降序，最多 25 条）")
        lines.append("")
        lines.append(f"| # | 分数 | 重要性 | 类型 | 记忆内容 |")
        lines.append(f"|---|------|--------|------|----------|")
        for mi, (mem, score) in enumerate(r["retrieved"]):
            imp = mem.importance_score
            mtype = mem.type
            # 去掉开头的 [Conversation Date: xxx]
            text = re.sub(r'^\[Conversation Date: [^\]]+\]\s*', '', mem.text_description)
            text = text.replace("|", "\\|").replace("\n", " ")
            if len(text) > 200:
                text = text[:197] + "..."
            lines.append(f"| {mi+1} | {score:.4f} | {imp:.1f} | {mtype} | {text} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n✅ 报告已生成 -> {OUTPUT_MD}")


if __name__ == "__main__":
    main()
