"""
LLM Debate & Provider Detection Engine
Auto-detects provider in priority:
1. claude_code (Claude Code CLI on PATH)
2. anthropic (ANTHROPIC_API_KEY)
3. openai (OPENAI_API_KEY)
Falls back cleanly to deterministic scoring when no key/login/network.
"""

import os
import json
import shutil
import subprocess
import requests
from typing import Dict, Any, Tuple, Optional
from scoring import evaluate_deterministic, collect_evidence_numbers, verify_grounding

SYSTEM_PROMPT = """You are an expert equity debate panel for Indian (NSE) stocks. The panel has six seats: Bull, Bear, Fundamentals, Technicals, News, and the Judge.
A BUY verdict requires genuinely favorable risk/reward with volume or momentum confirmation.
A WATCH verdict is for setups that are promising but unconfirmed.
An AVOID verdict is for poor risk/reward, downtrends, or distribution.

NON-NEGOTIABLE GROUNDING RULE:
Every figure cited MUST exist in the evidence bundle. Never invent numbers. If a needed value is missing, write "data unavailable".
Each agent's point must be <= 25 words. Judge rationale must be <= 2 lines.

Return ONLY a valid JSON object matching this exact format:
{
  "scores": {
    "bull": {"score": 75, "reasons": ["<=25-word grounded point"]},
    "bear": {"score": 30, "reasons": ["<=25-word grounded point"]},
    "technicals": {"score": 70, "reasons": ["<=25-word grounded point"]},
    "fundamentals": {"score": 85, "reasons": ["<=25-word grounded point"]},
    "news": {"score": 80, "reasons": ["<=25-word grounded point"]}
  },
  "verdict": {
    "winner": "Bull",
    "verdict": "BUY",
    "confidence": 8,
    "rationale": "Bullish accumulation backed by strong volume and positive earnings expectations.",
    "key_catalyst": "Breakout above 52-week position with rising relative volume.",
    "bull_score": 75,
    "bear_score": 30,
    "net": 45
  }
}
"""


# Cache failed provider status during process run to avoid repeated timeouts
_DISABLED_PROVIDERS = set()

def detect_provider() -> str:
    """
    Detects which LLM provider to use according to priority order.
    Honors LLM_PROVIDER env override if provided.
    """
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced in ["claude_code", "anthropic", "openai", "deterministic"]:
        if forced not in _DISABLED_PROVIDERS:
            return forced

    # Priority 1: Claude Code CLI
    if "claude_code" not in _DISABLED_PROVIDERS and shutil.which("claude") is not None:
        return "claude_code"

    # Priority 2: Anthropic API Key
    if "anthropic" not in _DISABLED_PROVIDERS and os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"

    # Priority 3: OpenAI API Key
    if "openai" not in _DISABLED_PROVIDERS and os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"

    return "deterministic"


def call_claude_code(prompt: str) -> Dict[str, Any]:
    """
    Calls claude CLI: claude -p "<prompt>" --output-format json with stdin=DEVNULL.
    Parses JSON envelope and returns result object.
    Raises RuntimeError on error so caller cleanly falls back.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=35
        )
    except Exception as e:
        raise RuntimeError(f"Claude CLI execution failed: {e}")

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(f"Claude CLI returned empty output (exit {proc.returncode})")

    try:
        envelope = json.loads(stdout)
    except Exception:
        raise RuntimeError(f"Claude CLI output is not valid JSON envelope: {stdout[:200]}")

    if envelope.get("is_error") is True:
        err_msg = envelope.get("result") or envelope.get("error") or "Unknown error"
        raise RuntimeError(f"Claude CLI returned error: {err_msg}")

    result_text = envelope.get("result", "")
    if isinstance(result_text, dict):
        return result_text

    # Extract JSON inside result if wrapped in markdown code fence
    cleaned = result_text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()

    return json.loads(cleaned)


def call_anthropic(prompt: str) -> Dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-3-5-haiku-latest",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}]
    }
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    text = data["content"][0]["text"].strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(text)


def call_openai(prompt: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    return json.loads(text)


def evaluate(evidence: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """
    Main evaluation entry point.
    Attempts LLM debate via detected provider.
    Cleanly falls back to deterministic if any error occurs.
    Enforces grounding verification.
    """
    provider = detect_provider()
    engine_used = provider

    if provider != "deterministic":
        user_prompt = f"{SYSTEM_PROMPT}\n\nEvidence bundle for {evidence.get('symbol')}:\n{json.dumps(evidence, indent=2)}"
        try:
            if provider == "claude_code":
                llm_output = call_claude_code(user_prompt)
            elif provider == "anthropic":
                llm_output = call_anthropic(user_prompt)
            elif provider == "openai":
                llm_output = call_openai(user_prompt)
            else:
                raise ValueError(f"Unknown provider: {provider}")

            # Validate expected fields
            if "scores" in llm_output and "verdict" in llm_output:
                v = llm_output["verdict"]
                # Normalize verdict fields
                v["winner"] = v.get("winner", "Bull")
                v["verdict"] = v.get("verdict", "WATCH").upper()
                v["confidence"] = max(1, min(10, int(v.get("confidence", 5))))
                v["rationale"] = v.get("rationale", "")
                v["key_catalyst"] = v.get("key_catalyst", "")
                v["bull_score"] = int(v.get("bull_score", 50))
                v["bear_score"] = int(v.get("bear_score", 50))
                v["net"] = v.get("net", v["bull_score"] - v["bear_score"])

                # Grounding verification
                allowed = collect_evidence_numbers(evidence)
                allowed.update([
                    float(v["bull_score"]), float(v["bear_score"]),
                    float(abs(v["net"])), float(v["net"]), float(v["confidence"])
                ])
                warnings = []
                warnings.extend(verify_grounding(v["rationale"], allowed))
                warnings.extend(verify_grounding(v["key_catalyst"], allowed))
                v["grounding_warnings"] = warnings

                return llm_output, f"LLM ({provider})"
        except Exception as e:
            # Clean fallback to deterministic and disable failing provider for this run
            print(f"LLM provider '{provider}' failed ({e}). Disabling and falling back to deterministic engine.")
            _DISABLED_PROVIDERS.add(provider)
            engine_used = "deterministic (fallback)"

    res = evaluate_deterministic(evidence)
    return res, "deterministic"
