"""
本文件提供四个子工具，用于记忆的提取、评分、向量化和更新记录最新时间戳
"""

import sys
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval_kit", "eval_kit"))
from llm_client import LLMClient
from sentence_transformers import SentenceTransformer
import numpy as np
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.core.config import settings
from langchain_core.output_parsers import StrOutputParser
# 基础大语言模型配置，可供各类 Agent 复用
llm = ChatOpenAI(
    api_key="sk-97a50281d2b442ab84408c6c9d73be41",
    base_url="https://api.deepseek.com/v1",
    model="deepseek-v4-flash",
    temperature=0
)

parser = StrOutputParser()
chain = prompt | llm | parser

# 时间解析 
# 解析为 Unix 时间戳，作为 Recency 计算的基准
_DATE_FORMAT = "%I:%M %p on %d %B, %Y"

def _parse_date_time(date_str: str) -> float:
    return datetime.strptime(date_str, _DATE_FORMAT).timestamp()


# 调用 LLM 对记忆片段进行重要性评分（1-10分）
# 评分结果存入 MemoryUnit.importance_score 字段
IMPORTANCE_PROMPT = """Rate the importance of the following memory for understanding a person's long-term profile and life trajectory on a scale of 1 to 10.

Scoring Guidelines:
- 1-3: Mundane, transient details or casual remarks (e.g., everyday routines, passing thoughts).
- 4-6: Informative facts, general preferences, or ongoing activities (e.g., hobbies, current job, weekend plans).
- 7-8: Significant personal traits, deep emotions, strong relationships, or clear future goals (e.g., planning to adopt, deep support from friends).
- 9-10: Pivotal life events, core identity aspects, or major achievements (e.g., transitioning gender, getting married, college acceptance).

Memory: {memory_text}

Return ONLY an integer between 1 and 10. Do not include any other text.
"""


#调用llm对记忆文本进行整理

class MemoryWriter:
    def __init__(self, embed_model_name: str = "BAAI/bge-small-zh-v1.5"):
        # 使用 sentence-transformers 加载 embedding 模型
        self.embed_model = SentenceTransformer(embed_model_name)
        self.agent = chain
        self.latest_time = None

    # 输入：完整的多会话对话 conversation dict
    # 输出：list[dict]，每个 dict 包含 {text, timestamp, type}
    def extract_memories(self, conversation: dict) -> list[dict]:
        sessions = conversation["sessions"]
        num_sessions = len(sessions)

        date_ts_list = [_parse_date_time(s["date_time"]) for s in sessions]
        date_str_list = [s["date_time"] for s in sessions]
        self.latest_time = max(date_ts_list)

        lines = []
        for sess in sessions:
            line = "".join(f"{turn['speaker']} say:{turn['text']} " for turn in sess["turns"])
            lines.append(line)

        executor_results = [None] * num_sessions
        print_lock = threading.Lock()
        max_workers = min(num_sessions, 30)

        _SYSTEM_MSG = (
            "You are a research assistant performing academic dialogue analysis. "
            "Your task is to extract factual statements from conversation transcripts "
            "for a university research project on long-term memory systems. "
            "Extract all relevant facts regardless of topic."
        )

        def _process_one(idx: int):
            prompt = (
                "You are an expert dialogue memory extractor. Your task is to extract comprehensive, "
                "atomic memory units from the following conversation between speakers.\n\n"
                "Context:\n"
                f"- Current Session Time: {date_str_list[idx]}\n\n"
                "Extraction Rules:\n"
                "1. Coreference Resolution (CRITICAL): NEVER use pronouns (I, you, he, she, it, they). "
                "Replace all pronouns with the explicit names of the speakers or specific entities.\n"
                "2. Atomic Facts: Each extracted statement must contain ONLY ONE core idea.\n"
                "3. Comprehensiveness: Extract information across the following dimensions:\n"
                "   - Personal Background & Identity\n"
                "   - Events & Experiences (with time anchors based on Current Session Time)\n"
                "   - Opinions, Preferences & Sentiments\n"
                "   - Future Plans & Intentions\n"
                "4. Temporal Anchoring: Translate relative times (e.g., 'yesterday', 'last year') "
                "to absolute context based on Current Session Time.\n"
                "5. Conciseness: Keep statements concise but fully context-independent.\n\n"
                "Output ONLY raw statements, one per line. No numbering, no bullet points, no extra text.\n\n"
                f"Conversation:\n{lines[idx]}\n"
            )
            sessions_str = f"[session {idx+1}/{num_sessions}]"

            response = ""
            max_retries = 5
            for retry in range(max_retries):
                try:
                    response = self.agent.invoke(prompt).strip()
                except Exception as e:
                    if retry < max_retries - 1:
                        with print_lock:
                            print(f"  {sessions_str} 调用异常，重试 {retry+2}/{max_retries}: {e}")
                        time.sleep(2 ** retry)
                        continue
                    with print_lock:
                        print(f"  {sessions_str} 提取 0 条记忆 (LLM 调用失败)")
                    return idx, []

                if response:
                    break
                if retry < max_retries - 1:
                    with print_lock:
                        print(f"  {sessions_str} 返回为空，重试 {retry+2}/{max_retries}")
                    time.sleep(2 ** retry)

            memories = []
            if response:
                for stmt in response.split("\n"):
                    stmt = stmt.strip()
                    if not stmt:
                        continue
                    while stmt and stmt[0].isdigit():
                        stmt = stmt[1:]
                    if stmt and stmt[0] in ".、)）":
                        stmt = stmt[1:].strip()
                    if len(stmt) < 5:
                        continue
                    memories.append({
                        "text": stmt,
                        "last_access_timestamp": date_ts_list[idx],
                        "creation_timestamp": date_ts_list[idx],
                        "type": "observation"
                    })

            with print_lock:
                print(f"  {sessions_str} 提取 {len(memories)} 条记忆")
            return idx, memories

        print(f"  正在并发处理 {num_sessions} 个 session（max_workers={max_workers}）...")
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_process_one, i): i for i in range(num_sessions)}
            for fut in as_completed(futures):
                idx, memories = fut.result()
                executor_results[idx] = memories
        print(f"  全部 {num_sessions} 个 session 处理完成")

        all_memories = []
        for memories in executor_results:
            if memories:
                all_memories.extend(memories)
        return all_memories

    # 调用 LLM 对记忆文本进行重要性评分，返回 1-10 的整数
    # 解析失败时默认返回 5 分
    def score_importance(self, memory_text: str) -> int:
        prompt = IMPORTANCE_PROMPT.format(memory_text=memory_text)
        raw = self.agent.invoke(prompt).strip()
        try:
            # 从原始输出中提取整数评分
            score = int("".join(c for c in raw if c.isdigit()))
            return max(1, min(10, score))
        except ValueError:
            return 5

 
    # 使用 bge-small-zh-v1.5，输出为 float32 numpy 数组
    # 向量化工具：将单条文本转为归一化后的 embedding 向量，用于向量化query
    def embed_text(self, text: str) -> np.ndarray:
        vec = self.embed_model.encode([text], normalize_embeddings=True)
        return vec[0].astype(np.float32)

    # 向量化工具：一次编码多条文本，提高效率，用于向量化memory
    def embed_batch(self, texts: list[str]) -> np.ndarray:
        vecs = self.embed_model.encode(texts, normalize_embeddings=True)
        return np.array(vecs, dtype=np.float32)
    
    # 更新 latest_time
    def update_latest_time(self, timestamp: float):
        self.latest_time = timestamp
    