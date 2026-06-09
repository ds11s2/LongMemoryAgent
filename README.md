# LongMemoryAgent - 长期记忆对话智能体

一个基于大语言模型的长期对话记忆系统，能够从多轮对话中提取、存储、检索和反思记忆，实现高质量的上下文感知问答。

## 📋 项目概览

本项目实现了一个完整的长期记忆对话Agent系统，核心功能包括：

- **记忆提取**：从对话中自动提取关键事实和信息
- **重要性评分**：使用LLM对记忆片段进行重要性评估（1-10分）
- **向量存储**：基于ChromaDB的高效记忆存储与检索
- **智能检索**：结合重要性和相关性的双因子检索策略
- **反思机制**：自动生成高层次洞察，提升记忆的抽象能力

## 🗂️ 目录结构

```
LongMemoryAgent/
├── memory_agent/                # 核心记忆Agent模块
│   ├── agent/                   # Agent控制器
│   │   ├── __init__.py
│   │   └── controller.py        # 主控制逻辑
│   ├── memory/                  # 记忆管理子系统
│   │   ├── __init__.py
│   │   ├── store.py             # ChromaDB存储实现
│   │   ├── writer.py            # 记忆提取与向量化
│   │   ├── retriever.py         # 双因子检索器
│   │   └── updater.py           # 反思更新机制
│   ├── eval/                    # 评测模块
│   │   ├── __init__.py
│   │   └── run_eval.py
│   ├── experiments/             # 实验结果
│   └── develop.md               # 开发文档
├── eval_kit/                    # 评测工具包
│   └── eval_kit/
│       ├── agent_template.py    # Agent接口模板
│       ├── vanilla_rag_agent.py # RAG基线实现
│       ├── llm_client.py        # LLM客户端
│       ├── metrics.py           # 评估指标
│       ├── prepare_eval_set.py  # 数据集准备
│       ├── run_generation.py    # 生成预测
│       └── run_judge.py         # LLM-as-Judge评测
├── benchmark_results/           # 评测结果
├── 文档/                        # 项目文档
├── requirements.txt             # 依赖清单
├── run_all_benchmarks.py        # 全量评测脚本
└── 指令.md                      # 中文说明
```

## 🚀 快速开始

### 环境要求

- Python 3.10+
- PyTorch 2.0+
- GPU显存 ≥ 8GB（推荐用于vLLM部署）

### 安装依赖

```bash
pip install -r requirements.txt

# 如需本地部署LLM（推荐）
pip install vllm
```

### 配置环境变量

创建 `.env` 文件（已加入 `.gitignore`）：

```env
# 生成模型（本地vLLM）
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=EMPTY
LLM_MODEL=Qwen/Qwen2.5-3B-Instruct-AWQ

# Judge模型（云端API）
AGENT_URL=https://api.deepseek.com/v1
AGENT_API_KEY=your-api-key-here
AGENT_MODEL=deepseek-v4-flash

# 可选：Embedding模型
EMBED_MODEL=BAAI/bge-small-zh-v1.5

# HuggingFace镜像（国内推荐）
HF_ENDPOINT=https://hf-mirror.com
```

### 启动vLLM服务

```bash
vllm serve Qwen/Qwen2.5-3B-Instruct-AWQ \
    --port 8000 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.75
```

## 🔧 使用方法

### 基础用法

```python
from memory_agent.agent.controller import MemoryAgent

# 初始化Agent
agent = MemoryAgent()

# 摄入对话历史
conversation = {
    "speaker_a": "Alice",
    "speaker_b": "Bob",
    "sessions": [
        {
            "session_id": 1,
            "date_time": "10:00 am on 1 January 2024",
            "turns": [
                {"speaker": "Alice", "text": "I love playing piano."},
                {"speaker": "Bob", "text": "That's great! How long have you been playing?"}
            ]
        }
    ]
}
agent.ingest(conversation)

# 回答问题
answer = agent.answer("What does Alice like to do?")
print(answer)  # "Alice likes playing piano."
```

### 运行全量评测

```bash
python run_all_benchmarks.py

# 可选参数
python run_all_benchmarks.py --only memory  # 只运行MemoryAgent
python run_all_benchmarks.py --skip nomem rag  # 跳过指定基准
```

## 🧠 核心架构

### 1. 记忆存储层 (MemoryStore)

使用ChromaDB持久化存储，每条记忆包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 唯一标识符 |
| `text_description` | str | 记忆的自然语言描述 |
| `type` | str | 类型：observation（观察）/ reflection（反思） |
| `creation_timestamp` | float | 创建时间戳 |
| `last_access_timestamp` | float | 最后访问时间戳 |
| `importance_score` | float | 重要性得分（1-10） |
| `embedding` | ndarray | 向量表示 |

### 2. 记忆写入器 (MemoryWriter)

**功能模块**：
- **extract_memories**: 从对话中提取结构化记忆（共指消解、去重）
- **score_importance**: LLM打分评估记忆重要性
- **embed_text/embed_batch**: 使用BGE模型向量化

### 3. 记忆检索器 (MemoryRetriever)

**双因子检索策略**：

| 因子 | 计算方式 | 权重 |
|------|----------|------|
| **Relevance** | 余弦相似度 | 80% |
| **Importance** | LLM评分归一化 | 20% |

**算法流程**：
1. 计算所有记忆的重要性分数和相关性分数
2. Min-Max归一化到[0,1]区间
3. 加权求和：`final = 0.8 × relevance + 0.2 × importance`
4. 排序取top-k返回

### 4. 记忆更新器 (MemoryUpdater)

**反思触发机制**：
- 累计新增记忆的importance_score超过阈值（默认25000）自动触发反思
- 取最近N条记忆（默认60条）生成高层次问题（默认3个）
- 对每个问题检索相关记忆，生成洞察（每问题3条）
- 洞察以reflection类型存入记忆库

**反思流程**：
```
记忆积累 → 触发阈值 → 生成问题 → 检索相关记忆 → 总结洞察 → 存储反思记忆
```

### 5. Agent控制器 (Controller)

**核心方法**：

```python
def ingest(conversation):
    # 提取记忆 → 向量化 → 评分 → 存储 → 累加重要性 → 检查反思

def answer(question):
    # 向量化问题 → 双因子检索 → 构建上下文 → LLM生成答案
```

## 📊 评测体系

### 评测基准

| 基准 | 描述 | 作用 |
|------|------|------|
| **NoMemory** | 完全不使用记忆 | 下界参考 |
| **FullContext** | 整段对话截断到模型上限 | 简单基线 |
| **VanillaRAG** | 逐轮切片+向量检索 | RAG基线 |
| **MemoryAgent** | 完整记忆系统 | 目标系统 |

### 评测指标

| 指标 | 说明 |
|------|------|
| **Judge Score** | LLM评分（CORRECT=1.0, PARTIAL=0.5, WRONG=0.0） |
| **F1** | Token级F1相似度 |
| **EM** | Exact Match精确匹配 |
| **Latency** | 平均回答延迟 |

### 运行评测

```bash
# 准备评测集
python eval_kit/eval_kit/prepare_eval_set.py \
    --output eval_set.json \
    --per_category 40

# 运行Agent生成预测
python eval_kit/eval_kit/run_generation.py \
    --eval_set eval_set.json \
    --agent memory_agent.agent.controller:MemoryAgent \
    --output predictions_memory.json

# LLM-as-Judge评测
python eval_kit/eval_kit/run_judge.py \
    --predictions predictions_memory.json \
    --output results_memory.json \
    --num_workers 4
```

## 🔬 可调参数

在 `controller.py` 的 `Settings` 类中集中管理：

```python
class Settings:
    # 检索参数
    retrieval_top_k: int = 150          # 检索返回条数
    
    # 反思触发
    reflection_threshold: int = 25000   # 触发反思的累计重要性阈值
    
    # 反思流程
    reflection_memory_limit: int = 60   # 反思时取最近记忆数
    reflection_question_count: int = 3  # 生成问题数量
    reflection_insight_per_q: int = 3  # 每问题生成洞察数
    reflection_retrieval_top_k: int = 30  # 反思检索条数
    
    # LLM参数
    reflection_max_tokens: int = 256
    reflection_temperature: float = 0
```

## 📝 文档资源

项目包含以下文档：

- `文档/记忆机制技术详解.md` - 记忆系统技术深度解析
- `文档/创新点分析文档.md` - 项目创新点说明
- `文档/NLP大作业_Proposal.tex` - 项目提案
- `memory_agent/develop.md` - 开发指南

## 📚 参考文献

1. Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents", ACL 2024
2. Park et al., "Generative Agents: Interactive Simulacra of Human Behavior", ArXiv 2023
3. "Mem0: Building Production-Ready AI Agents with Long-Term Memory"
4. "MemoryBank: Enhancing Large Language Models with Long-Term Memory"

## 📄 许可证

- 数据集：CC BY-NC 4.0（LoCoMo数据集）
- 代码：MIT License

---

*本项目为NLP课程大作业，实现了一个完整的长期记忆对话Agent系统。*