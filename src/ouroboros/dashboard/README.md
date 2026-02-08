# Ouroboros AI - Gemini 3 Context Demo

## 🏆 Gemini 3 Hackathon Submission

**Demonstrating Gemini 3's 1M Token Context for AI Self-Improvement**

### One-Liner
A visual dashboard showcasing how Gemini 3's unprecedented 1M token context window enables holistic analysis of AI iteration history that was impossible with previous models.

---

## 🎯 Problem Statement

Traditional LLMs with 4K-128K context windows can only analyze a fraction of an AI agent's iteration history. This leads to:
- **Local optimization** instead of global pattern recognition
- **Missed long-range dependencies** between distant iterations
- **Repeated mistakes** from forgotten history
- **Shallow Devil's Advocate analysis** without full context

## 💡 Solution: Full-Context Analysis

Ouroboros AI leverages Gemini 3's **1M token context** to analyze **50-200+ iterations** in a **single API call**, enabling:

1. **Holistic Pattern Recognition** - Detect patterns across the entire problem-solving trajectory
2. **Long-Range Dependency Analysis** - Identify connections between distant iterations
3. **Comprehensive Devil's Advocate** - Critical analysis with full historical context
4. **Multi-Dimensional Progress Tracking** - Visualize efficiency, coverage, and correctness over time

---

## 🚀 Key Features

### 1. Full History Analysis (AC1)
```python
from ouroboros.dashboard import GeminiContextAnalyzer

analyzer = GeminiContextAnalyzer()
result = await analyzer.analyze_full_history(
    iterations=history,  # 50-200+ iterations
    problem_context="Complex maze with items and enemies"
)
# Single API call analyzes ALL iterations
print(f"Tokens used: {result.value.token_count:,}")  # ~100K-500K tokens
```

### 2. API Logging System (AC2)
```python
from ouroboros.dashboard import GeminiAPILogger

logger = GeminiAPILogger()
await logger.initialize()

# All Gemini calls are logged with:
# - Request/response content
# - Token usage
# - Latency metrics
# - Real-time streaming for dashboard
```

### 3. Timeline Visualization (AC3)
- Interactive Plotly timeline of all iterations
- Color-coded phases (Discover/Define/Develop/Deliver)
- Insight overlay showing Gemini's discoveries
- Drill-down capability for individual iterations

### 4. Complex Maze Problem (AC4)
- **Shortest path finding** with dynamic obstacles
- **Item collection** with value optimization
- **Enemy avoidance** with patrol pattern prediction
- Generates 50+ realistic iteration data points

---

## 📊 Technical Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Streamlit Dashboard                       │
├─────────────────────────────────────────────────────────────┤
│  Timeline View  │  Metrics Charts  │  Devil's Advocate     │
└────────┬────────┴────────┬─────────┴──────────┬────────────┘
         │                 │                    │
         ▼                 ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│              GeminiContextAnalyzer                          │
│  • analyze_full_history() - 1M token context                │
│  • get_devil_advocate_analysis() - Critical review          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   GeminiAPILogger                           │
│  • SQLite persistence • Real-time streaming                 │
│  • Token tracking     • Latency metrics                     │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Gemini 3 API (2.5-pro)                     │
│              1,000,000 Token Context Window                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 🎬 Demo Flow (3 minutes)

### 0:00-0:30 - Introduction
- Problem: Limited context = local optimization
- Solution: Gemini 3's 1M tokens = holistic analysis

### 0:30-1:30 - Live Dashboard Demo
- Generate 60 iterations of maze-solving
- Watch Gemini analyze ALL iterations at once
- Show token counter (~30K-50K tokens used)

### 1:30-2:30 - Key Insights
- Timeline visualization with phase transitions
- Pattern detection across iterations
- Devil's Advocate critique highlighting root cause issues

### 2:30-3:00 - Impact & Conclusion
- This analysis was IMPOSSIBLE before Gemini 3
- Applications: Code review, debugging, optimization
- Future: Self-improving AI systems

---

## 🏅 Hackathon Criteria Alignment

### Technical Execution (40%)
- **Clean architecture**: Modular components with clear separation
- **Type safety**: Full type hints and Pydantic models
- **Error handling**: Result types for graceful failure
- **Testing**: Comprehensive test coverage
- **Documentation**: Detailed docstrings and README

### Innovation (30%)
- **Novel use of 1M context**: Not just "more tokens" but qualitatively different analysis
- **Holistic pattern recognition**: Detect patterns invisible to smaller contexts
- **Devil's Advocate at scale**: Critical analysis with complete history

### Impact (20%)
- **AI Self-Improvement**: Enable AI systems to learn from their full history
- **Debugging**: Find root causes in complex iteration traces
- **Optimization**: Identify inefficiencies across entire trajectories

### Presentation (10%)
- **Visual dashboard**: Interactive Streamlit interface
- **Clear metrics**: Token usage, latency, insight count
- **Compelling narrative**: Problem → Solution → Impact

---

## 🛠️ Installation

```bash
# Add streamlit and plotly to dependencies
pip install streamlit plotly pandas

# Run the dashboard
streamlit run src/ouroboros/dashboard/streamlit_app.py
```

## 📝 Environment Variables

```bash
# Required for Gemini API
export GOOGLE_API_KEY=your_api_key_here

# Optional: Use OpenRouter
export OPENROUTER_API_KEY=sk-or-your_key_here
```

---

## 🔬 Why Gemini 3 is Essential

| Feature | 4K Context | 128K Context | 1M Context (Gemini 3) |
|---------|-----------|--------------|----------------------|
| Iterations Analyzed | 2-5 | 20-50 | **200+** |
| Pattern Detection | Local | Regional | **Global** |
| Long-Range Dependencies | ❌ | Limited | **✓** |
| Full Devil's Advocate | ❌ | Partial | **✓** |
| Single API Call | ✓ | ✓ | **✓** |

---

## 📜 License

MIT License - Part of the Ouroboros AI Project

---

*Built for the Gemini 3 Hackathon - Demonstrating the power of 1M token context for AI self-improvement*
