#!/usr/bin/env python3
"""
快速测试脚本 —— 避免每次重新 ingest 数据。

用法：
  python test/run_test.py -ingest              # 只导入数据并持久化
  python test/run_test.py -run                 # 全量测试（有缓存则跳过 ingest）
  python test/run_test.py -run -change 5       # 并发 answer 数改为 5
  python test/run_test.py -run -change 1       # 串行 answer（观察日志用）
"""

import argparse
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 路径 ──
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

EVAL_SET_PATH = os.path.join(PROJECT_ROOT, "eval_kit", "eval_kit", "eval_set.json")
CHROMA_DB_DIR = os.path.join(PROJECT_ROOT, "chroma_db")
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
SAVED_DB_DIR = os.path.join(TEST_DIR, "saved_chroma_db")
PERSONA_FILE = os.path.join(TEST_DIR, "persona_summaries.txt")
PREDICTIONS_FILE = os.path.join(TEST_DIR, "predictions.json")
JUDGE_RESULTS_FILE = os.path.join(TEST_DIR, "judge_results.json")
RESULTS_MD_FILE = os.path.join(TEST_DIR, "results.md")

# 临时禁用 SentenceTransformer 在首次 import 时打印的进度条（保留其他信息）
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

print_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  从 ChromaDB 直接加载已有 collection（不删除、不重建）
# ═══════════════════════════════════════════════════════════════
def _load_store_from_disk(persist_dir: str):
    """打开一个已存在的 ChromaDB collection，返回 MemoryStore。"""
    import chromadb
    from memory_agent.memory.store import MemoryStore, COLLECTION_NAME

    store = MemoryStore.__new__(MemoryStore)
    store.client = chromadb.PersistentClient(path=persist_dir)
    store.collection = store.client.get_collection(COLLECTION_NAME)
    store._next_id = store.collection.count()
    return store


# ═══════════════════════════════════════════════════════════════
#  ingest 核心：一个 Agent 吞下全部对话
# ═══════════════════════════════════════════════════════════════
def ingest_all():
    from memory_agent.agent.controller import MemoryAgent

    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        eval_set = json.load(f)

    agent = MemoryAgent()
    total = len(eval_set)

    for i, sample in enumerate(eval_set):
        sample_id = sample["sample_id"]
        sessions_count = len(sample["conversation"]["sessions"])
        print(f"[ingest] ({i+1}/{total}) {sample_id}：正在导入 "
              f"{sessions_count} 个 session ...")
        agent.ingest(sample["conversation"])

    # ── 持久化 ──
    # 1. ChromaDB 目录
    import shutil
    if os.path.exists(SAVED_DB_DIR):
        shutil.rmtree(SAVED_DB_DIR)
    shutil.copytree(CHROMA_DB_DIR, SAVED_DB_DIR)
    print(f"[ingest] ChromaDB 已保存到 {SAVED_DB_DIR}")

    # 2. persona_summaries
    persona = getattr(agent, "persona_summaries", "")
    with open(PERSONA_FILE, "w", encoding="utf-8") as f:
        f.write(persona)
    print(f"[ingest] persona_summaries 已保存（{len(persona)} 字符）")

    print(f"[ingest] 全部完成，共 {total} 段对话。")
    return agent


# ═══════════════════════════════════════════════════════════════
#  run：有缓存就走缓存，没缓存先 ingest
# ═══════════════════════════════════════════════════════════════
def run_all(concurrency: int):
    from memory_agent.agent.controller import MemoryAgent

    # ── 加载 / 构建 agent ──
    cached = os.path.exists(SAVED_DB_DIR) and os.path.exists(PERSONA_FILE)

    if cached:
        print("[run] 检测到已有数据库，正在加载...")
        import shutil
        # MemoryAgent() 会清库，所以先备份 chroma_db（如果有），留到 init 后再恢复
        saved_before = None
        if os.path.exists(CHROMA_DB_DIR):
            saved_before = CHROMA_DB_DIR + ".backup_before_agent"
            if os.path.exists(saved_before):
                shutil.rmtree(saved_before)
            shutil.copytree(CHROMA_DB_DIR, saved_before)

        agent = MemoryAgent()                      # ← 这步会清空 chroma_db

        # 恢复之前的状态
        if saved_before:
            shutil.rmtree(CHROMA_DB_DIR)
            shutil.copytree(saved_before, CHROMA_DB_DIR)
            shutil.rmtree(saved_before)

        # 再用保存的缓存覆盖（保留 ingest 产出的完整数据）
        if os.path.exists(CHROMA_DB_DIR):
            shutil.rmtree(CHROMA_DB_DIR)
        shutil.copytree(SAVED_DB_DIR, CHROMA_DB_DIR)

        store = _load_store_from_disk(CHROMA_DB_DIR)
        agent.store = store
        agent.retriever.store = store
        agent.updater.store = store
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            agent.persona_summaries = f.read()
        print(f"[run] 已加载 {store.collection.count()} 条记忆 + persona 总结")
    else:
        print("[run] 未找到数据库缓存，开始导入数据...")
        agent = ingest_all()

    # ── 回答全部问题 ──
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        eval_set = json.load(f)

    all_qa = []
    for sample in eval_set:
        sample_id = sample["sample_id"]
        for qa in sample["qa_list"]:
            all_qa.append({
                **qa,
                "sample_id": sample_id,
            })

    total_qa = len(all_qa)
    predictions = [None] * total_qa
    qa_done = [0]
    conv_done_map = {}  # sample_id -> count done

    def _answer_one(idx: int):
        qa = all_qa[idx]
        t0 = time.time()
        try:
            pred = agent.answer(qa["question"])
            err = None
        except Exception as e:
            pred = ""
            err = f"answer_failed: {e}"
        latency = time.time() - t0

        result = {
            "qa_id": qa["qa_id"],
            "question": qa["question"],
            "reference": qa["answer"],
            "category": qa["category"],
            "category_name": qa["category_name"],
            "prediction": str(pred).strip(),
            "error": err,
            "latency_sec": round(latency, 3),
        }
        with print_lock:
            predictions[idx] = result
            qa_done[0] += 1
            sid = qa["sample_id"]
            conv_done_map[sid] = conv_done_map.get(sid, 0) + 1
            if qa_done[0] % 10 == 0 or qa_done[0] == total_qa:
                print(f"[run] 已回答 {qa_done[0]}/{total_qa} 题")

    print(f"[run] 开始并发回答 {total_qa} 题（并发数={concurrency}）...")
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(_answer_one, i) for i in range(total_qa)]
        for fut in as_completed(futures):
            fut.result()

    # 剔除 None（理论上不会有）
    predictions = [p for p in predictions if p is not None]

    with open(PREDICTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(predictions, f, ensure_ascii=False, indent=2)
    print(f"[run] 预测结果已保存 -> {PREDICTIONS_FILE}")

    # ── 运行 Judge ──
    print("[run] 正在运行 Judge 评测...")
    run_judge_subprocess()

    # ── 写 MD ──
    write_results_md()

    print(f"[run] 全部完成！结果见 {RESULTS_MD_FILE}")


# ═══════════════════════════════════════════════════════════════
#  Judge 子进程
# ═══════════════════════════════════════════════════════════════
def run_judge_subprocess():
    import subprocess

    eval_kit_dir = os.path.join(PROJECT_ROOT, "eval_kit", "eval_kit")
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = PROJECT_ROOT + (os.pathsep + pythonpath if pythonpath else "")

    cmd = [
        sys.executable, os.path.join(eval_kit_dir, "run_judge.py"),
        "--predictions", PREDICTIONS_FILE,
        "--output", JUDGE_RESULTS_FILE,
        "--num_workers", "4",
    ]
    subprocess.run(cmd, check=True, env=env)
    print(f"[run] Judge 结果已保存 -> {JUDGE_RESULTS_FILE}")


# ═══════════════════════════════════════════════════════════════
#  结果写入 MD
# ═══════════════════════════════════════════════════════════════
def write_results_md():
    with open(JUDGE_RESULTS_FILE, encoding="utf-8") as f:
        results = json.load(f)

    overall = results["overall"]
    by_cat = results["by_category"]
    model = results.get("judge_model", "unknown")

    lines = []
    lines.append(f"# 评测结果")
    lines.append(f"")
    lines.append(f"- **生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Judge 模型**：{model}")
    lines.append(f"- **总题数**：{overall['n']}")
    lines.append(f"")
    lines.append(f"## 总体指标")
    lines.append(f"")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|----|")
    lines.append(f"| 得分 (Score) | {overall['score']:.3f} |")
    lines.append(f"| F1 | {overall['f1']:.3f} |")
    lines.append(f"| EM | {overall['em']:.3f} |")
    lines.append(f"| 平均回答耗时 | {overall.get('avg_latency_sec', 0)}s |")
    lines.append(f"")
    lines.append(f"## 分类别结果")
    lines.append(f"")
    lines.append(f"| 类别 | 题数 | 得分 | F1 | EM | 正确 | 部分 | 错误 |")
    lines.append(f"|------|------|------|----|----|------|------|------|")
    for name in sorted(by_cat.keys()):
        d = by_cat[name]
        lines.append(
            f"| {name} | {d['n']} | {d['score']:.3f} | {d['f1']:.3f} | "
            f"{d['em']:.3f} | {d.get('correct', 0)} | "
            f"{d.get('partial', 0)} | {d.get('wrong', 0)} |"
        )
    lines.append(f"| **总体** | {overall['n']} | {overall['score']:.3f} | "
                 f"{overall['f1']:.3f} | {overall['em']:.3f} | | | |")

    with open(RESULTS_MD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[run] 结果 MD 已写入 -> {RESULTS_MD_FILE}")


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="快速测试脚本（缓存 ingest 数据，循环调参不用重新导入）")
    parser.add_argument("-ingest", action="store_true",
                        help="仅导入数据并持久化 DB")
    parser.add_argument("-run", action="store_true",
                        help="全量测试（有缓存则跳过 ingest）")
    parser.add_argument("-change", type=int, default=3,
                        help="修改 answer 并发数（默认 3）")
    args = parser.parse_args()

    if not args.ingest and not args.run:
        print("请指定 -ingest 或 -run 命令。用 -h 查看帮助。")
        sys.exit(1)

    if args.ingest:
        ingest_all()
    elif args.run:
        run_all(concurrency=args.change)
