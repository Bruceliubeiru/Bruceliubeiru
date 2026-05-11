"""
GEO Scoring Engine v2.

This module scores content based on AI-readability and GEO readiness.
"""

from dataclasses import dataclass, asdict
from typing import List, Dict


@dataclass
class GEODimensionScore:
    semantic_clarity: int
    citation_readiness: int
    faq_coverage: int
    comparison_readiness: int
    authority_signal: int
    ai_readability: int

    @property
    def total(self) -> int:
        return (
            self.semantic_clarity
            + self.citation_readiness
            + self.faq_coverage
            + self.comparison_readiness
            + self.authority_signal
            + self.ai_readability
        )


def _has_any(text: str, keywords: List[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def score_content(content: str) -> Dict:
    text = content.strip()
    lower = text.lower()
    word_count = len(text.split())

    semantic_clarity = 8
    if word_count > 80:
        semantic_clarity += 4
    if _has_any(lower, ["is", "means", "refers to", "helps", "solves"]):
        semantic_clarity += 4
    if _has_any(lower, ["who", "what", "why", "how"]):
        semantic_clarity += 2

    citation_readiness = 8
    if "." in text and word_count >= 30:
        citation_readiness += 4
    if _has_any(lower, ["because", "for example", "in short", "the key point"]):
        citation_readiness += 4

    faq_coverage = 5
    if "?" in text:
        faq_coverage += 8
    if _has_any(lower, ["faq", "question", "answer"]):
        faq_coverage += 5

    comparison_readiness = 5
    if _has_any(lower, ["vs", "versus", "compare", "alternative", "competitor", "difference"]):
        comparison_readiness += 10
    if "|" in text:
        comparison_readiness += 5

    authority_signal = 5
    if _has_any(lower, ["data", "case", "metric", "benchmark", "research", "evidence", "%"]):
        authority_signal += 10
    if any(char.isdigit() for char in text):
        authority_signal += 5

    ai_readability = 8
    if _has_any(text, ["#", "##", "- ", "1."]):
        ai_readability += 8
    if word_count < 400:
        ai_readability += 4

    score = GEODimensionScore(
        semantic_clarity=min(20, semantic_clarity),
        citation_readiness=min(20, citation_readiness),
        faq_coverage=min(15, faq_coverage),
        comparison_readiness=min(15, comparison_readiness),
        authority_signal=min(15, authority_signal),
        ai_readability=min(15, ai_readability),
    )

    recommendations = []
    if score.faq_coverage < 10:
        recommendations.append("Add FAQ questions and direct answers.")
    if score.comparison_readiness < 10:
        recommendations.append("Add comparison language or comparison table.")
    if score.authority_signal < 10:
        recommendations.append("Add proof points, cases, metrics, or benchmarks.")
    if score.citation_readiness < 14:
        recommendations.append("Use shorter, clearer, quote-ready sentences.")
    if score.semantic_clarity < 14:
        recommendations.append("Add a clear definition and target user description.")

    return {
        "geo_score": score.total,
        "max_score": 100,
        "breakdown": asdict(score),
        "recommendations": recommendations,
    }
