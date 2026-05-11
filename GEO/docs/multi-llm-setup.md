# Multi-LLM Setup Guide

## Goal

Configure OpenAI, Gemini, and DeepSeek using API keys.

Once configured, GEO Growth OS can switch providers dynamically.

## Supported Providers

| Provider | Status |
|---|---|
| OpenAI | Supported |
| Gemini | Supported |
| DeepSeek | Supported |

## Configuration

Set environment variables:

```bash
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY
```

## Example Usage

```python
from backend.llm_providers import MultiLLMClient

client = MultiLLMClient()

response = client.generate_text(
    provider="openai",
    prompt="Explain GEO simply"
)

print(response)
```

## Dynamic Provider Switching

Supported:

- openai
- gemini
- deepseek

Example:

```python
client.generate_text(provider="gemini", prompt="hello")
client.generate_text(provider="deepseek", prompt="hello")
```

## Strategic Value

This creates a provider abstraction layer.

Future GEO systems should not depend on a single LLM vendor.
