"""
Web Dashboard Server & Orchestration Engine
Flask backend, background execution thread, SQLite storage, Telegram alerts,
continuous live price streamer, and rich agent telemetry.
"""

import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
import html
import requests
from flask import Flask, jsonify, request, send_file

from data_sources import load_env, load_demo_data, fetch_live_data
from llm import evaluate

# Initialize environment
load_env(".env")

app = Flask(__name__)
DB_PATH = "stock_agent_audit.db"

# IST timezone helper (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p")


# Database Initialization
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            mode TEXT,
            universe_count INTEGER,
            shortlisted_count INTEGER,
            buy_count INTEGER,
            top_pick_symbol TEXT,
            top_pick_confidence INTEGER,
            engine TEXT,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            symbol TEXT,
            name TEXT,
            cap_segment TEXT,
            verdict TEXT,
            confidence INTEGER,
            winner TEXT,
            rationale TEXT,
            key_catalyst TEXT,
            bull_score INTEGER,
            bear_score INTEGER,
            net INTEGER,
            live_price REAL,
            day_change_pct REAL,
            evidence_json TEXT,
            created_at TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()


# Telegram Messenger Dispatcher (Clean, professional text formatting without emojis)
def send_telegram_alert(message_html: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram dispatch skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"Telegram message successfully delivered to chat {chat_id}.")
            return True
        else:
            print(f"Telegram dispatch failed: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"Telegram dispatch connection error: {e}")
        return False


def format_buy_signal_message(stock_data: dict, verdict_data: dict) -> str:
    symbol = html.escape(str(stock_data.get("symbol", "")))
    cap = html.escape(str(stock_data.get("cap_segment", "")).title())
    confidence = verdict_data.get("confidence", 7)
    winner = html.escape(str(verdict_data.get("winner", "Bull")))
    why = html.escape(str(verdict_data.get("rationale", "")))
    catalyst = html.escape(str(verdict_data.get("key_catalyst", "")))
    price = stock_data.get("price", {}).get("live", "N/A")
    change = stock_data.get("price", {}).get("day_change_pct", 0.0)
    change_str = f"{change:+.2f}%" if isinstance(change, (int, float)) else "N/A"

    msg = (
        f"[BUY SIGNAL] -- {symbol} ({cap} cap)\n\n"
        f"Verdict: BUY | Confidence: {confidence}/10\n\n"
        f"Winner: {winner}\n\n"
        f"Why: {why}\n\n"
        f"Key catalyst: {catalyst}\n\n"
        f"Live price: INR {price} | Day change: {change_str}\n\n"
        f"-- Analysis only. No trade was placed. Not investment advice."
    )
    return msg


def format_daily_summary_message(fired_signals: list, total_scanned: int, total_shortlisted: int, engine: str) -> str:
    timestamp_str = get_ist_now_str()
    lines = [
        "<b>[DAILY SCAN SUMMARY]</b>",
        f"Time: {timestamp_str} IST",
        f"Engine: {engine}",
        f"Universe Scanned: {total_scanned} | In Debate: {total_shortlisted}",
        f"Buy Signals Fired: {len(fired_signals)}\n"
    ]
    if fired_signals:
        lines.append("<b>Fired BUY Signals:</b>")
        for s in fired_signals:
            sym = html.escape(str(s.get("symbol", "")))
            conf = s.get("confidence", 7)
            price = s.get("live_price", 0.0)
            chg = s.get("day_change_pct", 0.0)
            lines.append(f"- <b>{sym}</b>: Confidence {conf}/10 (INR {price}, {chg:+.2f}%)")
    else:
        lines.append("No BUY signals fired.")

    lines.append("\n-- Analysis only. No trade was placed. Not investment advice.")
    return "\n".join(lines)


# Comprehensive Agent Metadata & Quantitative Specifications
AGENT_METADATA = {
    "scout": {
        "id": "scout",
        "name": "Scout",
        "icon": "radar",
        "role": "screens the stock universe for movers",
        "stat1_label": "Scanned",
        "stat2_label": "Shortlisted",
        "pipeline_order": 1,
        "description": "Scans hundreds of listed Indian equities in parallel across Large, Mid, and Small cap buckets to identify statistically significant intraday momentum candidates.",
        "methodology": "Fetches multi-day OHLCV candles using vectorized batch processing. Computes percentage day-change from prior regular session close and isolates the top gainers per capital segment for downstream committee analysis.",
        "inputs": ["NSE master universe (universe.json)", "5-day daily close series", "Intraday trading volume"],
        "outputs": ["Shortlisted candidates list", "Universe count", "Bucket rankings"],
        "rules": [
            "Screens Large Cap (NIFTY 100), Mid Cap (NIFTY Midcap 150), and Small Cap (NIFTY Smallcap 250).",
            "Sorts tickers descending by absolute day change percentage.",
            "Retains top SHORTLIST_PER_BUCKET (default 4) per market tier."
        ]
    },
    "technician": {
        "id": "technician",
        "name": "Technician",
        "icon": "chart",
        "role": "reads price action, RVOL & trend",
        "stat1_label": "Analyzed",
        "stat2_label": "Avg RVOL",
        "pipeline_order": 2,
        "description": "Extracts institutional volume footprint, moving average deviations, 52-week price structure, and intraday range closure.",
        "methodology": "Calculates Relative Volume (RVOL = session volume / 20-day baseline average volume). Measures distance to 20-day Simple Moving Average (SMA), 52-week range percentile (0% = 52W Low, 100% = 52W High), and intraday range positioning.",
        "inputs": ["1-month daily OHLCV dataframe", "52-week high & low prices", "Intraday session high/low/volume"],
        "outputs": ["RVOL ratio", "Price vs SMA percentage", "52W range percentile", "Trend classification (UP, DOWN, SIDEWAYS)"],
        "rules": [
            "RVOL >= 2.0x indicates significant institutional accumulation (+20 pts).",
            "52-week position >= 85% qualifies as breakout posture (+20 pts).",
            "Price > 2% above rising SMA confirms macro trend structure (+15 pts)."
        ]
    },
    "fundamentalist": {
        "id": "fundamentalist",
        "name": "Fundamentalist",
        "icon": "scales",
        "role": "weighs valuation & analyst targets",
        "stat1_label": "Covered",
        "stat2_label": "Avg upside",
        "pipeline_order": 3,
        "description": "Synthesizes institutional research coverage, mean consensus price targets, and recommendation distributions.",
        "methodology": "Calculates valuation headroom (percentage spread between current market price and consensus mean target price). Aggregates Strong Buy, Buy, Hold, and Sell recommendations into an analyst conviction index.",
        "inputs": ["targetMeanPrice", "targetHighPrice", "targetLowPrice", "numberOfAnalystOpinions", "recommendations breakdown"],
        "outputs": ["Target upside percentage", "Consensus recommendation key", "Coverage breadth", "Buy conviction ratio"],
        "rules": [
            "Analyst upside >= 15% denotes strong valuation cushion (+25 pts).",
            "Buy recommendations >= 80% denotes high institutional consensus (+15 pts).",
            "Zero or negative upside to consensus target triggers penalty (-20 pts)."
        ]
    },
    "newsdesk": {
        "id": "newsdesk",
        "name": "Newsdesk",
        "icon": "newspaper",
        "role": "pulls live news & scores sentiment",
        "stat1_label": "Headlines",
        "stat2_label": "Net tone",
        "pipeline_order": 4,
        "description": "Ingests real-time exchange announcements, corporate filings, and financial wire articles to detect sentiment bias.",
        "methodology": "Tokenizes news headlines through a financial sentiment lexicon (detecting earnings beats, capacity expansions, executive appointments, regulatory investigations, and debt defaults).",
        "inputs": ["Real-time wire headlines (ticker.news)", "Publisher metadata", "Release timestamps"],
        "outputs": ["Total headlines processed", "Positive sentiment tally", "Negative sentiment tally", "Net sentiment delta"],
        "rules": [
            "Net positive count (pos - neg >= 3) indicates strong market tailwinds.",
            "Negative headlines presence triggers cautionary sentiment score.",
            "Missing news flow defaults to neutral score (50/100)."
        ]
    },
    "bull": {
        "id": "bull",
        "name": "Bull",
        "icon": "trending-up",
        "role": "argues the case to buy",
        "stat1_label": "Cases",
        "stat2_label": "Avg score",
        "pipeline_order": 5,
        "description": "Constructs the offensive investment thesis, synthesizing upside catalysts into a quantitative 0-100 conviction score.",
        "methodology": "Evaluates cumulative strength of high RVOL, breakout positioning, rising trendlines, positive window returns, high analyst buy consensus, and constructive news flow.",
        "inputs": ["Technician indicators", "Fundamental targets", "Sentiment metrics"],
        "outputs": ["Bull conviction score (0-100)", "Primary upside catalysts", "Grounded thesis points"],
        "rules": [
            "Requires positive volume participation (RVOL >= 1.3x) to validate moves.",
            "Rewards breakout setups near 52-week highs.",
            "Strictly cites verified figures grounded in the evidence bundle."
        ]
    },
    "bear": {
        "id": "bear",
        "name": "Bear",
        "icon": "trending-down",
        "role": "argues the case against",
        "stat1_label": "Cases",
        "stat2_label": "Avg score",
        "pipeline_order": 6,
        "description": "Acts as risk manager and red team, scrutinizing distribution signs, lack of liquidity, overhead resistance, and downside exposures.",
        "methodology": "Calculates bearish conviction (0-100) based on sub-1.0 RVOL, trading near 52-week lows, negative moving average crossover, limited target headroom, and adverse news tone.",
        "inputs": ["Technician indicators", "Fundamental targets", "Sentiment metrics"],
        "outputs": ["Bear conviction score (0-100)", "Primary downside vulnerabilities", "Grounded risk points"],
        "rules": [
            "Heavy drawdown from 52-week high (<= -20%) penalizes score (+15 bear pts).",
            "Trading below 20-day SMA signifies active distribution pressure.",
            "Highlights weak intraday closes near session lows."
        ]
    },
    "judge": {
        "id": "judge",
        "name": "Judge",
        "icon": "shield-check",
        "role": "weighs the debate, issues verdict + confidence",
        "stat1_label": "Verdicts",
        "stat2_label": "Buy",
        "pipeline_order": 7,
        "description": "Presides over the Bull vs. Bear debate, computes net conviction delta, applies mandatory leadership checks, and issues binding verdicts.",
        "methodology": "Computes Net Conviction = Bull Score - Bear Score. Evaluates leadership criteria (52-week position >= 60% OR RVOL >= 3.0x). Sets confidence between 1 and 10 and enforces grounding verification on all cited figures.",
        "inputs": ["Bull thesis & score", "Bear thesis & score", "Evidence bundle", "Grounding verifier"],
        "outputs": ["Verdict: BUY | WATCH | AVOID", "Confidence (1-10)", "Executive rationale (<= 2 lines)", "Primary catalyst", "Grounding audit log"],
        "rules": [
            "BUY: Net Conviction >= 25 AND Leadership criteria met (52W Pos >= 60% OR RVOL >= 3.0x).",
            "AVOID: Net Conviction <= -15.",
            "WATCH: All setups falling between BUY and AVOID thresholds.",
            "Confidence: clamp(round(4 + net/15), 1, 10); forced >= 7 for BUY and <= 6 otherwise."
        ]
    },
    "messenger": {
        "id": "messenger",
        "name": "Messenger",
        "icon": "send",
        "role": "sends signals to Telegram",
        "stat1_label": "Sent",
        "stat2_label": "Engine",
        "pipeline_order": 8,
        "description": "Formats validated high-conviction BUY signals into standardized reports and dispatches them via Telegram Bot API with execution audit records.",
        "methodology": "Monitors verdicts for BUY signals where Confidence >= CONFIDENCE_THRESHOLD (default 7). Dispatches individual alerts followed by a daily pipeline summary, with complete token scrubbing.",
        "inputs": ["Judge verdicts", "Target chat configuration", "Confidence threshold"],
        "outputs": ["Dispatched alert count", "Telegram delivery status", "Audit database entry"],
        "rules": [
            "Dispatches only when verdict == BUY and confidence >= threshold.",
            "Scans and scrubs all bot tokens and secrets from UI and server logs.",
            "Emits a comprehensive summary message upon cycle termination."
        ]
    }
}

state_lock = threading.Lock()
app_state = {
    "status": "idle",
    "mode": "demo",
    "engine": "offline deterministic",
    "timestamp": get_ist_now_str(),
    "current_activity": "Ready. Click 'Start agents' to begin analysis.",
    "kpis": {
        "universe": 0,
        "in_debate": 0,
        "buy_signals": 0,
        "top_pick": None
    },
    "agents": {
        a["id"]: {
            "id": a["id"],
            "name": a["name"],
            "icon": a["icon"],
            "role": a["role"],
            "status": "offline",
            "stat1_label": a["stat1_label"],
            "stat1_val": "-",
            "stat2_label": a["stat2_label"],
            "stat2_val": "-"
        }
        for a in AGENT_METADATA.values()
    },
    "verdicts": [],
    "error": None
}


def reset_state(mode: str):
    with state_lock:
        app_state["status"] = "running"
        app_state["mode"] = mode
        app_state["timestamp"] = get_ist_now_str()
        app_state["current_activity"] = "Initializing agent panel..."
        app_state["error"] = None
        app_state["kpis"] = {
            "universe": 0,
            "in_debate": 0,
            "buy_signals": 0,
            "top_pick": None
        }
        for a in AGENT_METADATA.values():
            app_state["agents"][a["id"]]["status"] = "offline"
            app_state["agents"][a["id"]]["stat1_val"] = "-"
            app_state["agents"][a["id"]]["stat2_val"] = "-"
        app_state["verdicts"] = []


def run_pipeline(mode: str):
    delay = float(os.getenv("AGENT_DELAY", "0.4"))
    conf_threshold = int(os.getenv("CONFIDENCE_THRESHOLD", "7"))
    shortlist_per_bucket = int(os.getenv("SHORTLIST_PER_BUCKET", "4"))

    try:
        # Step 1: Scout Agent
        with state_lock:
            app_state["current_activity"] = "Scout is scanning market universe..."
            app_state["agents"]["scout"]["status"] = "working"
            app_state["agents"]["scout"]["stat1_val"] = "Scanning..."
            app_state["agents"]["scout"]["stat2_val"] = "-"
        time.sleep(delay)

        if mode == "live":
            universe_scanned, evidence_bundles = fetch_live_data("universe.json", shortlist_per_bucket)
        else:
            universe_scanned, evidence_bundles = load_demo_data("demo_data")

        shortlisted_count = len(evidence_bundles)
        with state_lock:
            app_state["agents"]["scout"]["stat1_val"] = str(universe_scanned)
            app_state["agents"]["scout"]["stat2_val"] = str(shortlisted_count)
            app_state["agents"]["scout"]["status"] = "done"
            app_state["kpis"]["universe"] = universe_scanned
            app_state["kpis"]["in_debate"] = shortlisted_count
            app_state["current_activity"] = f"Scout shortlisted {shortlisted_count} candidate stocks from {universe_scanned} listed companies."
        time.sleep(delay)

        # Step 2: Analysis Agents (Technician, Fundamentalist, Newsdesk)
        with state_lock:
            app_state["agents"]["technician"]["status"] = "working"
            app_state["agents"]["fundamentalist"]["status"] = "working"
            app_state["agents"]["newsdesk"]["status"] = "working"
            app_state["current_activity"] = "Technician, Fundamentalist & Newsdesk analyzing market data..."
        time.sleep(delay)

        rvols = [b.get("technicals", {}).get("rvol") for b in evidence_bundles if b.get("technicals", {}).get("rvol") is not None]
        avg_rvol = f"{sum(rvols)/len(rvols):.2f}x" if rvols else "1.00x"

        upsides = [b.get("analyst", {}).get("upside_pct") for b in evidence_bundles if b.get("analyst", {}).get("upside_pct") is not None]
        avg_upside = f"{sum(upsides)/len(upsides):+.1f}%" if upsides else "N/A"

        total_headlines = sum(b.get("news", {}).get("total", 0) for b in evidence_bundles)
        total_pos = sum(b.get("news", {}).get("positive", 0) for b in evidence_bundles)
        total_neg = sum(b.get("news", {}).get("negative", 0) for b in evidence_bundles)
        net_tone = f"{total_pos - total_neg:+d} net"

        with state_lock:
            app_state["agents"]["technician"]["stat1_val"] = str(shortlisted_count)
            app_state["agents"]["technician"]["stat2_val"] = avg_rvol
            app_state["agents"]["technician"]["status"] = "done"

            app_state["agents"]["fundamentalist"]["stat1_val"] = str(len(upsides))
            app_state["agents"]["fundamentalist"]["stat2_val"] = avg_upside
            app_state["agents"]["fundamentalist"]["status"] = "done"

            app_state["agents"]["newsdesk"]["stat1_val"] = str(total_headlines)
            app_state["agents"]["newsdesk"]["stat2_val"] = net_tone
            app_state["agents"]["newsdesk"]["status"] = "done"
            app_state["current_activity"] = "Technical, fundamental & sentiment data processed."
        time.sleep(delay)

        # Step 3: Debate & Judge (Bull, Bear, Judge)
        with state_lock:
            app_state["agents"]["bull"]["status"] = "working"
            app_state["agents"]["bear"]["status"] = "working"
            app_state["agents"]["judge"]["status"] = "working"
            app_state["current_activity"] = "Bull and Bear debating picks before the Judge..."

        all_verdicts = []
        bull_scores = []
        bear_scores = []
        buy_count = 0
        detected_engine = "deterministic"
        top_pick = None
        highest_conf = -1

        for idx, evidence in enumerate(evidence_bundles):
            sym = evidence.get("symbol", "UNKNOWN")
            with state_lock:
                app_state["current_activity"] = f"Evaluating debate for {sym} ({idx+1}/{len(evidence_bundles)})..."

            eval_res, engine = evaluate(evidence)
            detected_engine = engine

            scores = eval_res.get("scores", {})
            v = eval_res.get("verdict", {})

            b_score = v.get("bull_score", 50)
            br_score = v.get("bear_score", 50)
            bull_scores.append(b_score)
            bear_scores.append(br_score)

            verdict_str = v.get("verdict", "WATCH")
            conf = v.get("confidence", 5)

            if verdict_str == "BUY":
                buy_count += 1
                if conf > highest_conf:
                    highest_conf = conf
                    top_pick = {"symbol": sym, "confidence": conf}

            price_info = evidence.get("price", {})
            verdict_row = {
                "symbol": sym,
                "name": evidence.get("name", sym),
                "cap_segment": evidence.get("cap_segment", ""),
                "sector": evidence.get("sector", ""),
                "verdict": verdict_str,
                "confidence": conf,
                "winner": v.get("winner", "Bull"),
                "why": v.get("rationale", ""),
                "key_catalyst": v.get("key_catalyst", ""),
                "live_price": price_info.get("live", 0.0),
                "day_change_pct": price_info.get("day_change_pct", 0.0),
                "bull_score": b_score,
                "bear_score": br_score,
                "net": v.get("net", b_score - br_score),
                "scores": scores,
                "evidence": evidence,
                "grounding_warnings": v.get("grounding_warnings", [])
            }
            all_verdicts.append(verdict_row)

            with state_lock:
                app_state["verdicts"] = list(all_verdicts)
                app_state["kpis"]["buy_signals"] = buy_count
                if top_pick:
                    app_state["kpis"]["top_pick"] = top_pick
                app_state["agents"]["bull"]["stat1_val"] = str(len(bull_scores))
                app_state["agents"]["bull"]["stat2_val"] = f"{int(sum(bull_scores)/len(bull_scores))}/100"
                app_state["agents"]["bear"]["stat1_val"] = str(len(bear_scores))
                app_state["agents"]["bear"]["stat2_val"] = f"{int(sum(bear_scores)/len(bear_scores))}/100"
                app_state["agents"]["judge"]["stat1_val"] = str(len(all_verdicts))
                app_state["agents"]["judge"]["stat2_val"] = f"{buy_count} BUY"

            time.sleep(delay)

        with state_lock:
            app_state["agents"]["bull"]["status"] = "done"
            app_state["agents"]["bear"]["status"] = "done"
            app_state["agents"]["judge"]["status"] = "done"
            app_state["engine"] = detected_engine

        # Step 4: Messenger Agent (Telegram dispatch)
        with state_lock:
            app_state["agents"]["messenger"]["status"] = "working"
            app_state["current_activity"] = "Messenger verifying signals and dispatching Telegram alerts..."
        time.sleep(delay)

        fired_signals = []
        messages_sent_count = 0

        for row in all_verdicts:
            if row["verdict"] == "BUY" and row["confidence"] >= conf_threshold:
                fired_signals.append(row)
                buy_msg = format_buy_signal_message(row["evidence"], row)
                ok = send_telegram_alert(buy_msg)
                if ok:
                    messages_sent_count += 1
                time.sleep(0.3)

        summary_msg = format_daily_summary_message(
            fired_signals=fired_signals,
            total_scanned=universe_scanned,
            total_shortlisted=shortlisted_count,
            engine=detected_engine
        )
        summary_ok = send_telegram_alert(summary_msg)
        if summary_ok:
            messages_sent_count += 1

        has_telegram = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
        telegram_label = "Telegram" if has_telegram else "Mock/Logged"

        with state_lock:
            app_state["agents"]["messenger"]["stat1_val"] = str(messages_sent_count) if has_telegram else f"{len(fired_signals)} ready"
            app_state["agents"]["messenger"]["stat2_val"] = telegram_label
            app_state["agents"]["messenger"]["status"] = "done"
            app_state["status"] = "completed"
            app_state["timestamp"] = get_ist_now_str()
            app_state["current_activity"] = f"Analysis run completed. {len(fired_signals)} BUY signal(s) triggered."

        # Step 5: Save Run to SQLite Audit Log
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO runs (timestamp, mode, universe_count, shortlisted_count, buy_count, top_pick_symbol, top_pick_confidence, engine, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                app_state["timestamp"],
                mode,
                universe_scanned,
                shortlisted_count,
                buy_count,
                top_pick["symbol"] if top_pick else None,
                top_pick["confidence"] if top_pick else None,
                detected_engine,
                "completed"
            ))
            run_id = cur.lastrowid

            for v in all_verdicts:
                cur.execute("""
                    INSERT INTO verdicts (run_id, symbol, name, cap_segment, verdict, confidence, winner, rationale, key_catalyst, bull_score, bear_score, net, live_price, day_change_pct, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id,
                    v["symbol"],
                    v["name"],
                    v["cap_segment"],
                    v["verdict"],
                    v["confidence"],
                    v["winner"],
                    v["why"],
                    v["key_catalyst"],
                    v["bull_score"],
                    v["bear_score"],
                    v["net"],
                    v["live_price"],
                    v["day_change_pct"],
                    json.dumps(v["evidence"]),
                    app_state["timestamp"]
                ))
            conn.commit()
            conn.close()
            print(f"Run #{run_id} successfully logged to SQLite audit database.")
        except Exception as e:
            print(f"Error logging run to SQLite: {e}")

    except Exception as e:
        print(f"Pipeline error: {e}")
        with state_lock:
            app_state["status"] = "error"
            app_state["error"] = str(e)
            app_state["current_activity"] = f"Pipeline error: {e}"


# Continuous Live Price Worker (runs in background to keep active stock prices fresh)
def continuous_live_price_streamer():
    """
    Polls yfinance in the background every 8 seconds for all stocks currently displayed
    on the dashboard, streaming real-time price updates and day-change ticks continuously.
    """
    import yfinance as yf
    while True:
        try:
            symbols = []
            with state_lock:
                if app_state["verdicts"] and app_state["mode"] == "live":
                    symbols = [v["symbol"] for v in app_state["verdicts"]]

            if symbols:
                df = yf.download(symbols, period="1d", interval="1m", group_by="ticker", threads=True, progress=False)
                price_updates = {}
                for sym in symbols:
                    try:
                        sub = df if len(symbols) == 1 else (df[sym] if sym in df else None)
                        if sub is not None and not sub.empty:
                            closes = sub["Close"].dropna()
                            if len(closes) > 0:
                                last_val = round(float(closes.iloc[-1]), 2)
                                price_updates[sym] = last_val
                    except Exception:
                        pass

                if price_updates:
                    with state_lock:
                        for row in app_state["verdicts"]:
                            s = row["symbol"]
                            if s in price_updates:
                                new_price = price_updates[s]
                                row["live_price"] = new_price
                                prev_c = row.get("evidence", {}).get("price", {}).get("prev_close")
                                if prev_c and prev_c > 0:
                                    row["day_change_pct"] = round(((new_price - prev_c) / prev_c) * 100.0, 2)
                                if "price" in row.get("evidence", {}):
                                    row["evidence"]["price"]["live"] = new_price
                                    row["evidence"]["price"]["day_change_pct"] = row["day_change_pct"]
        except Exception as e:
            pass
        time.sleep(8)

streamer_thread = threading.Thread(target=continuous_live_price_streamer, daemon=True)
streamer_thread.start()


# Routes
@app.route("/")
def index():
    try:
        with open("dashboard.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading dashboard.html: {e}", 500


@app.route("/status", methods=["GET"])
def get_status():
    with state_lock:
        return jsonify(app_state)


@app.route("/live_prices", methods=["GET"])
def get_live_prices():
    with state_lock:
        prices = {}
        for v in app_state.get("verdicts", []):
            prices[v["symbol"]] = {
                "live_price": v.get("live_price"),
                "day_change_pct": v.get("day_change_pct")
            }
        return jsonify({
            "timestamp": get_ist_now_str(),
            "prices": prices
        })


@app.route("/agent_details/<agent_id>", methods=["GET"])
def get_agent_details(agent_id):
    agent_id = agent_id.lower().strip()
    if agent_id in AGENT_METADATA:
        meta = dict(AGENT_METADATA[agent_id])
        with state_lock:
            cur = app_state["agents"].get(agent_id, {})
            meta["status"] = cur.get("status", "offline")
            meta["stat1_val"] = cur.get("stat1_val", "-")
            meta["stat2_val"] = cur.get("stat2_val", "-")
        return jsonify(meta)
    return jsonify({"error": f"Agent {agent_id} not found"}), 404


@app.route("/agents_all", methods=["GET"])
def get_all_agents():
    res = {}
    with state_lock:
        for k, v in AGENT_METADATA.items():
            item = dict(v)
            cur = app_state["agents"].get(k, {})
            item["status"] = cur.get("status", "offline")
            item["stat1_val"] = cur.get("stat1_val", "-")
            item["stat2_val"] = cur.get("stat2_val", "-")
            res[k] = item
    return jsonify(res)


@app.route("/start", methods=["POST"])
def start_cycle():
    with state_lock:
        if app_state["status"] == "running":
            return jsonify({"status": "already_running", "message": "Pipeline is already in progress."}), 400

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "demo")
    if mode not in ["demo", "live"]:
        mode = "demo"

    reset_state(mode)
    thread = threading.Thread(target=run_pipeline, args=(mode,), daemon=True)
    thread.start()

    return jsonify({"status": "started", "mode": mode})


@app.route("/config", methods=["GET"])
def get_config():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return jsonify({
        "brand": os.getenv("BRAND", "ArthaAgent"),
        "confidence_threshold": int(os.getenv("CONFIDENCE_THRESHOLD", "7")),
        "shortlist_per_bucket": int(os.getenv("SHORTLIST_PER_BUCKET", "4")),
        "agent_delay": float(os.getenv("AGENT_DELAY", "0.4")),
        "has_telegram": bool(token and chat_id),
        "telegram_chat_id": f"***{chat_id[-4:]}" if len(chat_id) > 4 else ("configured" if chat_id else "not configured"),
        "llm_provider": os.getenv("LLM_PROVIDER", "auto-detect")
    })


if __name__ == "__main__":
    import socket

    def is_port_in_use(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", p)) == 0

    requested_port = int(os.getenv("PORT", "5001"))
    port = requested_port

    if is_port_in_use(port):
        if port == 5000:
            print("Notice: Port 5000 is in use (macOS AirPlay Receiver). Automatically switching to port 5001.")
            port = 5001
        while is_port_in_use(port):
            port += 1
        if port != requested_port and requested_port != 5000:
            print(f"Notice: Port {requested_port} is busy. Automatically switched to port {port}.")

    brand = os.getenv("BRAND", "ArthaAgent")
    print(f"Starting {brand} server on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
