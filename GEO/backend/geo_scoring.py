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


def _criterion(name: str, passed: bool, points: int, evidence: str) -> Dict:
    return {
        "name": name,
        "passed": passed,
        "points": points if passed else 0,
        "max_points": points,
        "evidence": evidence,
    }


def _score_criteria(items: List[Dict], max_score: int) -> int:
    return min(max_score, sum(item["points"] for item in items))


def score_content(content: str) -> Dict:
    text = content.strip()
    lower = text.lower()
    word_count = len(text.split())
    sentence_count = max(1, len([part for part in re_split_sentences(text) if part.strip()]))

    criteria = {
        "semantic_clarity": [
            _criterion("Clear definition", _has_any(lower, ["is", "means", "refers to", "what is"]), 5, "Uses definition-style wording."),
            _criterion("Target user or use case", _has_any(lower, ["who", "for ", "user", "traveler", "customer", "适合", "人群"]), 5, "Names who the content is for."),
            _criterion("Problem or value explained", _has_any(lower, ["helps", "solves", "benefit", "why", "value", "解决", "优势"]), 5, "Explains value or problem solved."),
            _criterion("Enough context", word_count >= 80, 3, f"{word_count} words detected."),
            _criterion("Question framing", _has_any(lower, ["what", "why", "how", "which", "如何", "为什么"]), 2, "Contains user-question framing."),
        ],
        "citation_readiness": [
            _criterion("Complete sentences", "." in text or "。" in text, 5, "Has sentence boundaries."),
            _criterion("Quote-ready length", sentence_count >= 2 and word_count >= 30, 5, f"{sentence_count} sentences detected."),
            _criterion("Reasoning markers", _has_any(lower, ["because", "for example", "in short", "the key point", "原因", "例如"]), 4, "Contains explainable reasoning markers."),
            _criterion("Specific claims", any(char.isdigit() for char in text) or _has_any(lower, ["coverage", "route", "price", "rule", "范围", "价格", "路线"]), 4, "Contains specific claim signals."),
            _criterion("Concise enough", word_count <= 800, 2, f"{word_count} words detected."),
        ],
        "faq_coverage": [
            _criterion("Questions present", "?" in text or "？" in text, 5, "Contains explicit questions."),
            _criterion("FAQ labels", _has_any(lower, ["faq", "question", "answer", "常见问题", "问答"]), 4, "Contains FAQ or Q&A labels."),
            _criterion("How-to intent", _has_any(lower, ["how", "use", "redeem", "book", "如何", "使用", "预订"]), 3, "Covers usage intent."),
            _criterion("Comparison intent", _has_any(lower, ["which", "worth", "compare", "哪个", "值不值", "对比"]), 3, "Covers decision intent."),
        ],
        "comparison_readiness": [
            _criterion("Comparison wording", _has_any(lower, ["vs", "versus", "compare", "difference", "对比", "区别"]), 5, "Contains comparison wording."),
            _criterion("Alternatives", _has_any(lower, ["alternative", "competitor", "option", "其他", "替代", "选择"]), 4, "Mentions options or alternatives."),
            _criterion("Selection logic", _has_any(lower, ["which", "choose", "best for", "适合", "怎么选"]), 4, "Contains selection guidance."),
            _criterion("Table-like structure", "|" in text or "\t" in text, 2, "Contains table-like formatting."),
        ],
        "authority_signal": [
            _criterion("Data or metrics", _has_any(lower, ["data", "metric", "benchmark", "%", "数据", "指标"]), 4, "Contains data or metrics."),
            _criterion("Examples or cases", _has_any(lower, ["case", "example", "review", "案例", "评价", "示例"]), 4, "Contains examples, cases, or reviews."),
            _criterion("Numbers present", any(char.isdigit() for char in text), 3, "Contains numeric signals."),
            _criterion("Evidence language", _has_any(lower, ["research", "evidence", "source", "proof", "证明", "来源"]), 4, "Contains evidence/source language."),
            _criterion("Trust or policy clarity", _has_any(lower, ["policy", "rule", "eligibility", "restriction", "规则", "限制"]), 4, "Explains trust, rule, or eligibility details."),
        ],
        "ai_readability": [
            _criterion("Headings or markdown", _has_any(text, ["#", "##", "###"]), 4, "Uses heading markers."),
            _criterion("Bullets or numbered steps", _has_any(text, ["- ", "1.", "2.", "•"]), 4, "Uses list formatting."),
            _criterion("Short enough for extraction", word_count <= 400, 4, f"{word_count} words detected."),
            _criterion("Structured separators", _has_any(text, [":", "：", "|"]), 3, "Uses labels or separators."),
            _criterion("Readable density", sentence_count >= 2 and word_count / sentence_count <= 45, 4, "Average sentence length is manageable."),
        ],
    }

    score = GEODimensionScore(
        semantic_clarity=_score_criteria(criteria["semantic_clarity"], 20),
        citation_readiness=_score_criteria(criteria["citation_readiness"], 20),
        faq_coverage=_score_criteria(criteria["faq_coverage"], 15),
        comparison_readiness=_score_criteria(criteria["comparison_readiness"], 15),
        authority_signal=_score_criteria(criteria["authority_signal"], 15),
        ai_readability=_score_criteria(criteria["ai_readability"], 15),
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
        "criteria": criteria,
        "recommendations": recommendations,
    }


def re_split_sentences(text: str) -> List[str]:
    import re

    return re.split(r"[.!?。！？]+", text)
