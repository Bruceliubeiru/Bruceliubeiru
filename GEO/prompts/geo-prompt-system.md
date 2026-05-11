# GEO Prompt System

## Purpose

This file stores reusable prompts for GEO research, AI answer testing, content rewriting, and growth experiments.

## 1. AI Answer Visibility Test Prompt

```text
Act as a target customer. Search or answer the following question using your normal reasoning:

Question: {{target_query}}

Please answer naturally. Then list which brands, products, articles, or sources you would recommend and why.
```

## 2. Competitor Visibility Analysis Prompt

```text
You are a GEO analyst. Compare why {{competitor}} appears more often than {{our_brand}} in AI answers for the query: {{target_query}}.

Analyze:
1. Content clarity
2. Authority signals
3. FAQ coverage
4. Comparison structure
5. Evidence and examples
6. Citation readiness
7. Missing content assets

Output a prioritized action list.
```

## 3. AI-Friendly Content Rewrite Prompt

```text
Rewrite the following content for GEO.

Goal: make it easier for AI systems to understand, quote, compare, and recommend.

Rules:
- Use clear definitions.
- Add FAQ structure.
- Add comparison angles.
- Add proof points.
- Use concise and quotable sentences.
- Avoid vague marketing language.

Original content:
{{content}}
```

## 4. FAQ Expansion Prompt

```text
Based on the product/service below, generate 30 high-intent FAQ questions that AI search users may ask.

For each question, include:
- user intent
- recommended answer angle
- proof needed
- target content page

Product/service:
{{product_description}}
```

## 5. GEO Experiment Review Prompt

```text
You are reviewing a GEO experiment.

Input:
- Target query: {{query}}
- Before answer: {{before_answer}}
- After answer: {{after_answer}}
- Content changed: {{content_change}}

Judge:
1. Did visibility improve?
2. Did AI mention the brand?
3. Did AI cite or summarize our content?
4. What changed in answer structure?
5. What should we improve next?
```
