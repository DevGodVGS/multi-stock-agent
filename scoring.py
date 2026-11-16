"""
Scoring & Debate Engine (Deterministic Implementation + Grounding Verifier)
Provides the fallback evaluation and grounds all cited figures.
"""

import re
import math
from typing import Dict, Any, List, Tuple, Set, Optional


def collect_evidence_numbers(bundle: Dict[str, Any]) -> Set[float]:
    """
    Recursively extracts all numeric values present in the evidence bundle,
    including their rounded representations, to enable strict grounding verification.
    """
    numbers: Set[float] = set()

    def _extract(obj):
        if isinstance(obj, (int, float)):
            if not math.isnan(obj) and not math.isinf(obj):
                f = float(obj)
                numbers.add(f)
                numbers.add(round(f, 1))
                numbers.add(round(f, 2))
                numbers.add(round(f, 0))
        elif isinstance(obj, dict):
            for v in obj.values():
                _extract(v)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)

    _extract(bundle)
    # Common domain constants allowed in text
    numbers.update([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 20.0, 50.0, 52.0, 100.0])
    return numbers


def verify_grounding(text: str, allowed_numbers: Set[float]) -> List[str]:
    """
    Checks if all numbers cited in the text can be traced back to the evidence bundle.
    Returns any untraced numbers as warnings.
    """
    if not text:
        return []
    warnings = []
    # Match numbers like 14.5, -16.8, 85, 20%
    matches = re.findall(r"[-+]?\b\d+(?:\.\d+)?", text)
    for m in matches:
        try:
            val = float(m)
            # Check if val or rounded variations exist in allowed
            matched = any(
                abs(val - allowed) < 0.05 or abs(val - allowed) / (abs(allowed) + 1e-6) < 0.02
                for allowed in allowed_numbers
            )
            if not matched and val not in allowed_numbers:
                warnings.append(f"Untraced figure '{m}' in output text")
        except ValueError:
            pass
    return warnings


def evaluate_technician(evidence: Dict[str, Any]) -> Dict[str, Any]:
    tech = evidence.get("technicals", {})
    r_52w = evidence.get("range_52w", {})
    rvol = tech.get("rvol")
    sma_pct = tech.get("price_vs_sma_pct")
    pos_52w = r_52w.get("position_pct")
    day_pos = tech.get("day_range_position_pct")
    trend = tech.get("trend", "sideways")
    win_ret = tech.get("window_return_pct")

    score = 50
    reasons = []

    if rvol is not None:
        if rvol >= 2.0:
            score += 20
            reasons.append(f"High RVOL at {rvol:.2f}x indicates institutional buying volume")
        elif rvol >= 1.2:
            score += 10
            reasons.append(f"Above-average RVOL at {rvol:.2f}x")
        elif rvol < 0.8:
            score -= 15
            reasons.append(f"Muted RVOL of {rvol:.2f}x shows below-average participation")
    else:
        reasons.append("RVOL data unavailable")

    if pos_52w is not None:
        if pos_52w >= 80:
            score += 15
            reasons.append(f"Near 52-week highs with position at {pos_52w:.1f}%")
        elif pos_52w < 35:
            score -= 15
            reasons.append(f"Lagging near 52-week low at {pos_52w:.1f}%")
    else:
        reasons.append("52-week position data unavailable")

    if sma_pct is not None:
        if sma_pct > 2.0 and trend == "up":
            score += 15
            reasons.append(f"Price is {sma_pct:+.1f}% above SMA in confirmed uptrend")
        elif sma_pct < -2.0:
            score -= 15
            reasons.append(f"Price is {sma_pct:+.1f}% below SMA under distribution pressure")
    else:
        reasons.append("SMA benchmark data unavailable")

    score = max(5, min(95, score))
    return {"score": score, "reasons": reasons[:2]}


def evaluate_fundamentalist(evidence: Dict[str, Any]) -> Dict[str, Any]:
    analyst = evidence.get("analyst", {})
    upside_pct = analyst.get("upside_pct")
    buy_pct = analyst.get("buy_pct")
    num_analysts = analyst.get("num_analysts")
    consensus = analyst.get("consensus")

    score = 50
    reasons = []

    if upside_pct is not None:
        if upside_pct >= 15.0:
            score += 25
            reasons.append(f"Substantial analyst target upside of {upside_pct:.1f}%")
        elif upside_pct >= 5.0:
            score += 12
            reasons.append(f"Moderate analyst target upside of {upside_pct:.1f}%")
        elif upside_pct <= 0:
            score -= 20
            reasons.append(f"Zero or negative headroom to consensus target ({upside_pct:.1f}%)")
    else:
        reasons.append("Target price data unavailable")

    if buy_pct is not None:
        if buy_pct >= 80.0:
            score += 15
            reasons.append(f"High analyst consensus with {buy_pct:.0f}% buy recommendations")
        elif buy_pct < 50.0:
            score -= 15
            reasons.append(f"Low analyst conviction with only {buy_pct:.0f}% buy ratings")
    else:
        reasons.append("Analyst recommendation breakdown data unavailable")

    score = max(5, min(95, score))
    return {"score": score, "reasons": reasons[:2]}


def evaluate_newsdesk(evidence: Dict[str, Any]) -> Dict[str, Any]:
    news = evidence.get("news", {})
    total = news.get("total", 0)
    pos = news.get("positive", 0)
    neg = news.get("negative", 0)

    score = 50
    reasons = []

    if total == 0:
        reasons.append("No recent news headlines available")
        return {"score": 50, "reasons": reasons}

    net = pos - neg
    if net >= 3 or (pos > 0 and neg == 0):
        score = min(90, 60 + pos * 5)
        reasons.append(f"Strong positive sentiment across {pos} positive headlines")
    elif net <= -2 or (neg > 0 and pos == 0):
        score = max(10, 40 - neg * 8)
        reasons.append(f"Cautionary sentiment with {neg} negative headlines detected")
    else:
        score = 52
        reasons.append(f"Balanced news coverage ({pos} positive, {neg} negative)")

    return {"score": score, "reasons": reasons[:2]}


def evaluate_bull(evidence: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Bull: + high RVOL (capped), breakout (52w position >= 85), price above a rising SMA,
    strong day-range close, analyst upside >= 10%, >= 80% buy, positive news, positive window return.
    """
    tech = evidence.get("technicals", {})
    r_52w = evidence.get("range_52w", {})
    analyst = evidence.get("analyst", {})
    news = evidence.get("news", {})

    rvol = tech.get("rvol")
    pos_52w = r_52w.get("position_pct")
    sma_pct = tech.get("price_vs_sma_pct")
    trend = tech.get("trend")
    day_pos = tech.get("day_range_position_pct")
    win_ret = tech.get("window_return_pct")
    upside = analyst.get("upside_pct")
    buy_pct = analyst.get("buy_pct")
    pos_news = news.get("positive", 0)
    neg_news = news.get("negative", 0)

    score = 15
    reasons = []

    # High RVOL (capped)
    if rvol is not None and rvol >= 1.3:
        pts = min(20, int(rvol * 8))
        score += pts
        reasons.append(f"Strong RVOL at {rvol:.2f}x demonstrates institutional accumulation")

    # Breakout (52w position >= 85)
    if pos_52w is not None and pos_52w >= 85.0:
        score += 20
        reasons.append(f"Breakout structure with 52-week position at {pos_52w:.1f}%")
    elif pos_52w is not None and pos_52w >= 70.0:
        score += 10
        reasons.append(f"Constructive 52-week range positioning at {pos_52w:.1f}%")

    # Price above rising SMA
    if sma_pct is not None and sma_pct > 0:
        if trend == "up":
            score += 15
            reasons.append(f"Price is {sma_pct:+.1f}% above moving average in an active uptrend")
        else:
            score += 8
            reasons.append(f"Price is holding {sma_pct:+.1f}% above moving average")

    # Strong day-range close
    if day_pos is not None and day_pos >= 70.0:
        score += 12
        reasons.append(f"Strong intraday close at {day_pos:.1f}% of day range")

    # Analyst upside >= 10%
    if upside is not None and upside >= 10.0:
        score += 15
        reasons.append(f"Attractive analyst target upside of {upside:.1f}%")

    # >= 80% buy
    if buy_pct is not None and buy_pct >= 80.0:
        score += 10
        reasons.append(f"Overwhelming analyst endorsement with {buy_pct:.0f}% buy ratings")

    # Positive news
    if pos_news > neg_news:
        score += 10
        reasons.append(f"Favorable news backdrop with {pos_news} positive reports")

    # Positive window return
    if win_ret is not None and win_ret > 0:
        score += 10
        reasons.append(f"Sustained momentum with {win_ret:+.1f}% monthly window return")

    clamped_score = max(5, min(100, score))
    return clamped_score, reasons


def evaluate_bear(evidence: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Bear: + RVOL < 1, near 52w low (< 30%), below SMA / downtrend, no analyst headroom (upside <= 0),
    low conviction (buy < 55%), far from high (<= -20%), high sell %, negative news, weak close.
    """
    tech = evidence.get("technicals", {})
    r_52w = evidence.get("range_52w", {})
    analyst = evidence.get("analyst", {})
    news = evidence.get("news", {})

    rvol = tech.get("rvol")
    pos_52w = r_52w.get("position_pct")
    pct_from_high = r_52w.get("pct_from_high")
    sma_pct = tech.get("price_vs_sma_pct")
    trend = tech.get("trend")
    day_pos = tech.get("day_range_position_pct")
    upside = analyst.get("upside_pct")
    buy_pct = analyst.get("buy_pct")
    sell_pct = analyst.get("sell_pct")
    pos_news = news.get("positive", 0)
    neg_news = news.get("negative", 0)

    score = 15
    reasons = []

    # RVOL < 1
    if rvol is not None and rvol < 1.0:
        score += 15
        reasons.append(f"Sluggish RVOL of {rvol:.2f}x signals lack of fresh buying appetite")

    # Near 52w low (< 30%)
    if pos_52w is not None and pos_52w < 30.0:
        score += 20
        reasons.append(f"Vulnerable price action near 52-week low at {pos_52w:.1f}%")

    # Below SMA / downtrend
    if (sma_pct is not None and sma_pct < 0) or trend == "down":
        score += 20
        reasons.append(f"Under pressure below moving average with downward trajectory")

    # No analyst headroom (upside <= 0)
    if upside is not None and upside <= 0:
        score += 15
        reasons.append(f"No valuation headroom with target upside at {upside:.1f}%")

    # Low conviction (buy < 55%)
    if buy_pct is not None and buy_pct < 55.0:
        score += 10
        reasons.append(f"Tepid analyst backing with only {buy_pct:.0f}% buy ratings")

    # Far from high (<= -20%)
    if pct_from_high is not None and pct_from_high <= -20.0:
        score += 15
        reasons.append(f"Heavy drawdown of {pct_from_high:.1f}% from 52-week peak")

    # High sell %
    if sell_pct is not None and sell_pct >= 20.0:
        score += 10
        reasons.append(f"Notable sell consensus at {sell_pct:.0f}% of coverage")

    # Negative news
    if neg_news > pos_news:
        score += 10
        reasons.append(f"Adverse sentiment flow with {neg_news} negative headlines")

    # Weak close (day_range_position_pct <= 25)
    if day_pos is not None and day_pos <= 25.0:
        score += 10
        reasons.append(f"Intraday rejection closing near session lows at {day_pos:.1f}%")

    clamped_score = max(5, min(100, score))
    return clamped_score, reasons


def evaluate_deterministic(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure deterministic scoring engine conforming exactly to Section 5(B).
    Must ALWAYS work with no LLM / network.
    """
    bull_score, bull_reasons = evaluate_bull(evidence)
    bear_score, bear_reasons = evaluate_bear(evidence)
    tech_eval = evaluate_technician(evidence)
    fund_eval = evaluate_fundamentalist(evidence)
    news_eval = evaluate_newsdesk(evidence)

    net = bull_score - bear_score

    # Spec Section 5(B) Judge rules:
    # BUY if net >= 25 AND leadership (52w position >= 60 OR RVOL >= 3);
    # AVOID if net <= -15; else WATCH.
    # Confidence = clamp(round(4 + net/15), 1, 10), forced >= 7 for BUY and <= 6 otherwise.
    pos_52w = evidence.get("range_52w", {}).get("position_pct") or 0.0
    rvol = evidence.get("technicals", {}).get("rvol") or 0.0

    has_leadership = (pos_52w >= 60.0 or rvol >= 3.0)

    if net >= 25 and has_leadership:
        verdict = "BUY"
    elif net <= -15:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    # Confidence calculation
    raw_conf = round(4 + net / 15.0)
    clamped_conf = max(1, min(10, raw_conf))

    if verdict == "BUY":
        confidence = max(7, clamped_conf)
    else:
        confidence = min(6, clamped_conf)

    # Determine winner
    if net > 5:
        winner = "Bull"
    elif net < -5:
        winner = "Bear"
    else:
        winner = "Neutral"

    # Key catalyst (grounded top reason)
    key_catalyst = ""
    if verdict == "BUY":
        key_catalyst = bull_reasons[0] if bull_reasons else "Favorable risk-reward with confirmed volume"
    elif verdict == "AVOID":
        key_catalyst = bear_reasons[0] if bear_reasons else "Unfavorable downtrend and poor demand profile"
    else:
        key_catalyst = (bull_reasons[0] if bull_reasons else "Consolidating price structure pending breakout confirmation")

    # Rationale (<= 2 lines)
    if verdict == "BUY":
        rationale = f"Bull conviction ({bull_score}/100) dominates with confirmed momentum. {key_catalyst}."
    elif verdict == "AVOID":
        rationale = f"Bear conviction ({bear_score}/100) highlights distribution risks. {key_catalyst}."
    else:
        rationale = f"Conflicting signals with net conviction {net:+d}. Promising setup but awaits volume confirmation before committing capital."

    # Grounding check: include evidence numbers as well as generated scores
    allowed_numbers = collect_evidence_numbers(evidence)
    allowed_numbers.update([float(bull_score), float(bear_score), float(abs(net)), float(net), float(confidence)])
    grounding_warnings = []
    grounding_warnings.extend(verify_grounding(rationale, allowed_numbers))
    grounding_warnings.extend(verify_grounding(key_catalyst, allowed_numbers))
    for r in bull_reasons + bear_reasons:
        grounding_warnings.extend(verify_grounding(r, allowed_numbers))

    result = {
        "scores": {
            "bull": {"score": bull_score, "reasons": bull_reasons[:2]},
            "bear": {"score": bear_score, "reasons": bear_reasons[:2]},
            "technicals": tech_eval,
            "fundamentals": fund_eval,
            "news": news_eval
        },
        "verdict": {
            "winner": winner,
            "verdict": verdict,
            "confidence": confidence,
            "rationale": rationale,
            "key_catalyst": key_catalyst,
            "bull_score": bull_score,
            "bear_score": bear_score,
            "net": net,
            "grounding_warnings": grounding_warnings
        }
    }
    return result
