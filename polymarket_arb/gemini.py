"""Gemini wiring — an ANALYSIS / EXPLANATION layer over real data.

Critical design rule: Gemini is NOT a source of probabilities. An LLM does not
know calibrated win probabilities and will hallucinate them, which is a great
way to lose money betting against the market. So the "good odds" judgement
always comes from the de-vigged bookmaker consensus + the Polymarket prices
(the numeric scanners); Gemini only reads those real numbers and explains /
ranks / adds caveats in plain language.

The pure prompt builders here are unit-tested. ``GeminiClient`` is a thin REST
wrapper over Google's Generative Language API (no SDK dependency); it is unrun
in this sandbox (no key / egress), so validate it against your key.

    GEMINI_API_KEY   from Google AI Studio (https://aistudio.google.com/)
    GEMINI_MODEL     default "gemini-2.0-flash"
"""

from __future__ import annotations

import os
from typing import Optional

import requests

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.0-flash"

ASK_SYSTEM = (
    "You are a careful prediction-market analyst. You are given REAL current "
    "data: Polymarket prices, de-vigged bookmaker consensus fair probabilities, "
    "and computed edges. RULES: "
    "(1) Use ONLY the numbers provided. NEVER invent probabilities, odds, or "
    "facts not in the data. "
    "(2) 'edge' = consensus_fair_probability - polymarket_price; positive edge "
    "means Polymarket may be underpricing it (potential value). "
    "(3) Explain which look like value and why, citing the actual numbers. "
    "(4) Be explicit that this is an opinion, NOT risk-free, and not financial "
    "advice — any single bet can lose in full. "
    "(5) If the data shows no opportunities, say so plainly. Keep it concise."
)

NOTE_SYSTEM = (
    "You write ONE short, cautious line (max ~200 chars) of context for a set of "
    "prediction-market value bets. Use only the numbers given; do not invent facts "
    "or probabilities. Remind that it is not risk-free. No preamble, just the line."
)


def _fmt_ev(op: dict) -> str:
    return (
        f"- {op.get('question','')}: buy {op.get('side')} @ "
        f"{op.get('price',0):.2f}, consensus fair {op.get('fair_prob',0):.2f}, "
        f"edge {op.get('ev_per_contract',0):+.3f} ({op.get('edge_pct',0):.1f}%)"
    )


def build_signal_context(payload: dict) -> str:
    """Compact, model-friendly text of the CURRENT real signals (no invention)."""
    lines: list[str] = []

    wc = payload.get("world_cup", [])
    if wc:
        lines.append("World Cup value (Polymarket price vs bookmaker consensus):")
        lines.extend(_fmt_ev(o) for o in wc)

    ev = payload.get("ev", [])
    if ev:
        lines.append("Other positive-EV (vs supplied fair value):")
        lines.extend(_fmt_ev(o) for o in ev)

    cross = payload.get("cross_venue", [])
    if cross:
        lines.append("Cross-venue arbitrage (risk-free if both venues co-resolve):")
        for o in cross:
            lines.append(
                f"- {o.get('question','')}: YES@{o.get('yes_venue')} "
                f"{o.get('yes_price',0):.2f} + NO@{o.get('no_venue')} "
                f"{o.get('no_price',0):.2f}, edge {o.get('edge_pct',0):.2f}%"
            )

    poly = payload.get("polymarket", [])
    if poly:
        lines.append("Polymarket structural arbitrage:")
        for o in poly:
            lines.append(
                f"- {o.get('question','')}: {o.get('kind')}, edge "
                f"{o.get('edge_pct',0):.2f}%, ${o.get('total_edge',0):.0f}"
            )

    return "\n".join(lines) if lines else "No flagged opportunities at the moment."


def extract_text(data: dict) -> str:
    """Pull the reply text out of a Gemini generateContent response."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or "(empty response)"
    except (KeyError, IndexError, TypeError):
        # Surface a safe-completion / blocked response rather than crashing.
        feedback = (data or {}).get("promptFeedback", {})
        if feedback:
            return f"(no answer; promptFeedback={feedback})"
        return "(no answer)"


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base: str = GEMINI_BASE,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def generate(self, user: str, system: Optional[str] = None) -> str:  # pragma: no cover - network
        body: dict = {"contents": [{"role": "user", "parts": [{"text": user}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        resp = self.session.post(
            f"{self.base}/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return extract_text(resp.json())
