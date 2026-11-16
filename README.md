# ArthaAgent · Multi-Agent Indian Stock Analysis Dashboard

A local, one-click web dashboard that runs a panel of 8 named autonomous agents to analyze Indian (NSE) stocks, debate each pick with an LLM, and automatically dispatch BUY signals to Telegram. Runs entirely locally on your machine with zero cloud backend.

---

## Architecture Overview

```
                 +---------------------------------------------+
                 |               Web Dashboard                 |
                 |      (Vanilla HTML5, CSS3, JS · No CDNs)     |
                 +---------------------------------------------+
                                       ▲
                        polls /status (500ms)
                                       ▼
                 +---------------------------------------------+
                 |             Flask Orchestrator              |
                 |           (Background Run Thread)           |
                 +---------------------------------------------+
                                       |
    +----------------------------------+----------------------------------+
    |                                  |                                  |
    ▼                                  ▼                                  ▼
[Data Layer]                  [Debate & Scoring]                 [Signal Dispatcher]
• Demo Mode (offline bundles) • Auto-detects Claude Code CLI     • Telegram Bot API
• Live NSE Data via yfinance  • Anthropic API / OpenAI API      • Strict BUY format
• Evidence Bundle Builder     • Deterministic Rule Fallback      • Daily Run Summary
                              • Strict Grounding Verifier        • SQLite Audit Log
```

### The 8 Pipeline Agents

| ID | Name | Role | Stat 1 | Stat 2 |
|---|---|---|---|---|
| `scout` | Scout | screens the stock universe for movers | Scanned | Shortlisted |
| `technician` | Technician | reads price action, RVOL & trend | Analyzed | Avg RVOL |
| `fundamentalist` | Fundamentalist | weighs valuation & analyst targets | Covered | Avg upside |
| `newsdesk` | Newsdesk | pulls live news & scores sentiment | Headlines | Net tone |
| `bull` | Bull | argues the case to buy | Cases | Avg score |
| `bear` | Bear | argues the case against | Cases | Avg score |
| `judge` | Judge | weighs the debate, issues verdict + confidence | Verdicts | Buy |
| `messenger` | Messenger | sends signals to Telegram | Sent | Engine |

---

## 10. How to Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **LLM with no API key:**
   Install Claude Code, run `claude`, and `/login` with your Claude plan — the app auto-detects the CLI on your `PATH`.
   *(Or set an API key in `.env` using `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`).*
   If no LLM or key is present, the app automatically falls back to the deterministic scoring engine.

3. **Telegram setup:**
   Get a bot token from [@BotFather](https://t.me/BotFather), and your chat ID from [@userinfobot](https://t.me/userinfobot).
   Put both in your `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
   TELEGRAM_CHAT_ID=987654321
   ```
   *(If omitted, signals are displayed in the UI and logged locally).*

4. **Launch the application:**
   ```bash
   python app.py
   ```
   Open [http://127.0.0.1:5001](http://127.0.0.1:5001) in your browser and click **Start agents**.
   - Use **Demo** first for instant offline analysis.
   - Use **Live** during market hours (NSE: Mon–Fri 09:15–15:30 IST).
   *(Note: Default port is set to `5001` to avoid conflicts with macOS AirPlay Receiver on port 5000).*

---

## Key Features

- **One-Click End-to-End Execution**: A single click orchestrates all 8 agents through `offline` -> `working` -> `done` with visual pacing.
- **Dual Engine Architecture**:
  1. **LLM Debate (Preferred)**: Shells out to local `claude` CLI (using your Claude subscription with no API key or per-call costs) or uses Anthropic / OpenAI APIs.
  2. **Deterministic Fallback**: Built-in 0–100 quantitative rule-based evaluation that works 100% offline with zero dependencies.
- **Strict Grounding Rule**: Built-in numeric verifier ensures agents never hallucinate financial figures; all cited numbers must trace back directly to the verified evidence bundle.
- **Telegram BUY Signal Format**:
  ```
  🟢 BUY SIGNAL — SYMBOL (Large cap)

  Verdict: BUY | Confidence: 8/10

  Winner: Bull

  Why: Bull conviction (75/100) dominates with confirmed momentum...

  Key catalyst: Breakout structure with 52-week position at 94.7%

  Live price: ₹1340.50 | Day change: +0.06%

  — Analysis only. No trade was placed. Not investment advice.
  ```
- **Auditable SQLite Storage**: Every run and every stock verdict is persisted to `stock_agent_audit.db`.
- **Security & Privacy**: Telegram bot tokens and API keys are completely scrubbed from all logs, UI elements, and API payloads.

*(Data source is yfinance for portability; swap `data_sources.py` for a richer feed — broker API, paid data, or an MCP connector — if you have one. Keep the evidence-bundle shape identical so the scoring engine is unchanged.)*
