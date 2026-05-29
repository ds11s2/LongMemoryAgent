"""
本文件实现了记忆的反思更新：
sum_score：累计所有新记忆的 importance_score，超过 150 触发反思
reflect 方法：触发反思流程，取最近 100 条记忆生成高阶见解
reflect_agent：反思的具体实现 —— 生成问题 → 检索 → 总结洞察
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval_kit", "eval_kit"))

from llm_client import LLMClient
from memory_agent.memory.store import MemoryStore


# 将最近 100 条记忆喂给 LLM，生成 3 个高层次问题
REFLECT_PROMPT = """You are given the following recent memories:

{memory_list}

From these statements, generate exactly 3 high-level insightful questions that could be answered by synthesizing the information above.

Format: one question per line, no numbering.
Just return the questions, don't contain any other text.
"""


# 对每个问题检索到的信息，LLM 总结出 3 条高层次洞察
# 格式示例："Klaus Mueller is dedicated to his research on gentrification (because 1,2,8,15)"
REFLECT_INSIGHT_PROMPT = """Based on the following retrieved information:
{retrieved_info}
From the above, infer exactly 3 high-level insights. Format each as:
"Insight (because 1,2,3)" where the numbers reference the statements above.

Example: "Klaus Mueller is dedicated to his research on gentrification (because 1,2,8,15)"
Format: one insight per line, no numbering.
Just return the insights, don't contain any other text.
"""


# 累计重要性超过 150 时触发 reflect
REFLECTION_THRESHOLD = 150

class MemoryUpdater:
    def __init__(self, store: MemoryStore, retriever=None, writer=None):
        self.store = store
        self.retriever = retriever
        self.writer = writer
        self.llm = LLMClient()
        # 累加每条新记忆的重要性分数
        self._accumulated_importance: float = 0.0

    # 当 sum_score >= 150 时触发
    def check_and_reflect(self, current_time: float = None) -> bool:
        if self._accumulated_importance < REFLECTION_THRESHOLD:
            return False
        if self.retriever is None or self.writer is None:
            return False

        import time
        if current_time is None:
            current_time = time.time()

        #  取最近 100 条记忆，按创建时间倒序
        memories = self.store.get_all()
        recent_memories = sorted(memories, key=lambda m: m.creation_timestamp, reverse=True)[:100]

        if not recent_memories:
            return False
        # 将 100 条记忆格式化后喂给 LLM
        memory_list = "\n".join(
            f"{i+1}. {m.text_description}" for i, m in enumerate(recent_memories)
        )

        # reflect_agent 输出3 个高层次问题
        questions_raw = self.llm.generate(
            REFLECT_PROMPT.format(memory_list=memory_list),
            max_tokens=256, temperature=0.2,
        )
        questions = [q.strip() for q in questions_raw.strip().split("\n") if q.strip()][:3]

        # 对每个问题做检索再总结洞察
        all_insights = []
        for question in questions:
            # 对问题调用 retrieval 工具进行三因子检索，检索 20 条记忆
            query_emb = self.writer.embed_text(question)
            retrieved = self.retriever.retrieve(question, query_emb, top_k=20, current_time=current_time)
            retrieved_info = "\n".join(f"{i+1}. {m.text_description}" for i, (m, _) in enumerate(retrieved))
            
            # 基于检索结果总结 3 条高层次洞察
            # 格式如："Klaus Mueller 致力于他关于绅士化的研究（因为 1,2,8,15）"
            insights_raw = self.llm.generate(
                REFLECT_INSIGHT_PROMPT.format(retrieved_info=retrieved_info),
                max_tokens=256, temperature=0.2,
            )
            insight_lines = [l.strip() for l in insights_raw.strip().split("\n") if l.strip()][:3]
            all_insights.extend(insight_lines)

        # 将 9 条反思记忆写入数据库
        # 存入时 type 标记为 "reflection"
        for insight_text in all_insights:
            importance = self.writer.score_importance(insight_text)
            embedding = self.writer.embed_text(insight_text)
            self.store.add(
                text=insight_text,
                memory_type="reflection",
                importance_score=importance,
                embedding=embedding,
                timestamp=current_time,
            )

        # 触发反思后减150
        self._accumulated_importance -= REFLECTION_THRESHOLD
        return True

    # 每写入一条记忆就累加其 importance 到 sum_score
    # 然后自动检查是否触发 reflect
    def add_importance(self, score: float, current_time: float = None):
        self._accumulated_importance += score
        if current_time is None:
            import time
            current_time = time.time()
        self.check_and_reflect(current_time)
