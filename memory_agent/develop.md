## 调用逻辑

评测系统会调用 `ingest` 方法存入记忆，当调用 `ingest` 方法时，`controller` 会依次：

> 1. 调用 `writer.py` 的 `extract_memories` 提取所有 sessions 的文本（按 session 合并 turns 为一个字符串），可以理解为将每一轮对话提取为一条字符串
> 2. 调用 `writer.py` 的 `embed_batch` 批量向量化每一轮对话的字符串
> 3. 逐条调用 `writer.py` 的 `score_importance`（即 `mark_agent`）评分，再调用 `store.py` 的 `add` 存入数据库

### 每条记忆的存储格式

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识符 |
| `text_description` | 记忆的自然语言描述（例如："Klaus 正在阅读一本关于士绅化的书"） |
| `type` | 记忆类型（观察 `observation` / 反思 `reflection`） |
| `creation_timestamp` | 创建时间戳 |
| `last_access_timestamp` | 最后一次被访问的时间戳 |
| `importance_score` | 重要性得分（1到10分） |
| `embedding` | 文本的向量表示 |

### 反思触发

同时，`ingest` 方法每写入一条记忆都会调用 `updater.py` 的 `add_importance` 累加 `sum_score`（`_accumulated_importance`），总得分超过阈值（默认 150 分）时自动调用 `updater.py` 的 `check_and_reflect` 方法进行反思，并将总得分减去阈值。

### `check_and_reflect` 反思逻辑

取最近若干条记忆（默认 100 条），按创建时间倒序，喂给 LLM 令其生成若干高层次问题（默认 3 个），再对每个问题调用 `retriever.py` 的 `retrieve` 方法进行三因子检索，最后对每个问题检索到的信息调用 LLM 总结出若干条高层次洞察（默认每问题 3 条，共 9 条），格式如：

> "关于 Klaus Mueller 的陈述
> 1.Klaus Mueller 正在写一篇研究论文
> 2.Klaus Mueller 喜欢阅读一本关于绅士化的书
> 3.Klaus Mueller 正在与 Ayesha Khan 讨论锻炼 [...]
> 从上述陈述中你能推断出哪 3 个高层次的见解？（示例格式：见解（因为 1,5,3））"

这个过程生成诸如 **"Klaus Mueller 致力于他关于绅士化的研究（因为 1,2,8,15）"** 这样的陈述，而后对每条洞察调用 `writer` 的 `score_importance` 评分和 `embed_text` 向量化，再调用 `store` 的 `add` 以 `"reflection"` 类型存入数据库。

---

## 三重检索（`retrieve` 方法）

### 1. 维度 A：Recency（近期性 / 衰减度）

- **概念**：越近发生的事情，或者越近刚刚回想起来的事情，越容易被记住。
- **算法**：论文采用指数衰减函数。
  - 衰减系数 = **0.995**
  - 公式：**0.995^Δt**，其中 Δt 是自上次访问该记忆以来经过的单位时间数。

> **这个计算方法在这里有很大问题**，因为论文是在一个沙盒中模拟二十个智能体，沙盒中有时间的变动，且时间是均匀变动的，而此项目一次性提供了所有记忆的创建时间，计算肯定不能以我们现实时间为作为当前时间，目前是以对话中最晚的时间为当前时间进行计算，这显然不是最优解。

### 2. 维度 B：Importance（重要性）

- **概念**：区分日常琐事（比如刷牙）和核心记忆（比如分手、考上大学）。
- **算法**：借助 LLM 打分。在记忆刚刚生成并存入数据库时，就已经生成。提示词如下：

> "在 1 到 10 的范围内，1 代表纯粹的日常琐事（如刷牙、叠被子），10 代表极其深刻的经历（如分手、大学录取），请为以下记忆片段评估其可能的心酸/重要程度。
> 记忆内容：[在柳树市场买杂货]
> 评分：\<填入数字\>"

（得到分数后，存入数据库的 `importance_score` 字段。）

### 3. 维度 C：Relevance（相关性）

- **概念**：当前发生的事，和哪些历史记忆相关。比如你在讨论化学考试，早饭吃了什么就不相关。
- **算法**：计算当前 Query 文本的 Embedding 向量与数据库中所有记忆的 Embedding 向量的**余弦相似度** (Cosine Similarity)。

### 综合得分计算

这三个分数原本的值域不同（比如相关性是 0-1，重要性是 1-10），需要用 **Min-Max Scaling（归一化）**，如果出现了 max 和 min 相同的，则加一个极小值保护：

> `(X - Min) / (Max - Min + 1e-5)`

把它们都缩放到 `[0, 1]` 区间，然后相加：

> **Final_Score = (1.0 × Recency) + (1.0 × Importance) + (1.0 × Relevance)**

根据这个 `Final_Score` 进行倒序排序，在大模型的上下文限制内取足够多的排名靠前的记忆组合成字符串，返回这个字符串，同时，对于被检索到的记忆，将它在数据库中的 `last_access_timestamp` 更新为当前时间。

---

> **在 `controller.py` 中的 `setting` 类中包含了全部影响记忆效果的参数，可以直接在 `setting` 类中修改然后进行测试。**
