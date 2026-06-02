"""
本文件实现了基于双因子打分的记忆检索
- 维度 A：Importance（重要性）—— LLM 打分
- 维度 B：Relevance（相关性）—— 余弦相似度
- Min-Max 归一化后加权求和（Relevance 占 80%，Importance 占 20%），排序取 top_k
"""

import numpy as np
from memory_agent.memory.store import MemoryStore, MemoryUnit


class MemoryRetriever:
    def __init__(self, store: MemoryStore, embed_model=None, writer=None):
        self.store = store
        self.embed_model = embed_model
        self.writer = writer

    def _importance_score(self, mem: MemoryUnit) -> float:
        return float(mem.importance_score)

    # Relevance（相关性）
    # 计算 query embedding 与记忆 embedding 的余弦相似度
    # 由于 embedding 已做 normalize，使用 dot product 等价于 cosine similarity
    def _relevance_score(self, query_embedding: np.ndarray, mem: MemoryUnit) -> float:
        if mem.embedding is None or query_embedding is None:
            return 0.0
        sim = float(np.dot(mem.embedding, query_embedding))
        return max(0.0, sim)

    # Min-Max 归一化
    # 两个维度的值域不同（Relevance 0-1, Importance 1-10），
    @staticmethod
    def _min_max_normalize(values: list[float]) -> list[float]:
        if not values:
            return []
        min_v = min(values)
        max_v = max(values)
        # 用 Min-Max Scaling 缩放到 [0, 1]，加 1e-5 防止除零错误
        return [(v - min_v) / (max_v - min_v + 1e-5) for v in values]

    # 综合检索
    # retrieval 方法主入口
    def retrieve(self, query: str, query_embedding: np.ndarray, top_k: int = 5) -> list[tuple[MemoryUnit, float]]:
        memories = self.store.get_all()
        if not memories:
            return []
        # 对所有记忆分别计算Importance、Relevance 分数
        importance_scores = [self._importance_score(m) for m in memories]
        relevance_scores = [self._relevance_score(query_embedding, m) for m in memories]
        # 对Importance、Relevance 分数进行 Min-Max 归一化
        norm_importance = self._min_max_normalize(importance_scores)
        norm_relevance = self._min_max_normalize(relevance_scores)
        # 对所有记忆的Importance、Relevance 分数进行加权求和（Relevance:Importance = 8:2）
        final_scores = []
        for i, mem in enumerate(memories):
            score = 0.2 * norm_importance[i] + 0.8 * norm_relevance[i]
            final_scores.append((mem, score))
        # 按综合分数排序，取 top_k 个记忆单位
        final_scores.sort(key=lambda x: x[1], reverse=True)
        return final_scores[:top_k]
