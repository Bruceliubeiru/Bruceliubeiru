"""
Multi-LLM Provider Layer for GEO Growth OS.

Supported providers:
- OpenAI
- Gemini
- DeepSeek

Usage:
Set API keys in .env and call generate_text(provider, prompt).
"""

import os
from typing import Literal

from openai import OpenAI

ProviderName = Literal["openai", "gemini", "deepseek"]


class LLMProviderError(Exception):
    pass


class MultiLLMClient:
    def __init__(self):
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")

    def _openai_client(self):
        if not self.openai_api_key:
            raise LLMProviderError("OPENAI_API_KEY is missing")
        return OpenAI(api_key=self.openai_api_key)

    def _deepseek_client(self):
        if not self.deepseek_api_key:
            raise LLMProviderError("DEEPSEEK_API_KEY is missing")
        return OpenAI(
            api_key=self.deepseek_api_key,
            base_url="https://api.deepseek.com"
        )

    def generate_text(
        self,
        provider: ProviderName,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.3,
    ) -> str:
        if provider == "openai":
            return self._generate_openai(prompt, model or "gpt-4.1-mini", temperature)

        if provider == "deepseek":
            return self._generate_deepseek(prompt, model or "deepseek-chat", temperature)

        if provider == "gemini":
            return self._generate_gemini(prompt, model or "gemini-1.5-flash", temperature)

        raise LLMProviderError(f"Unsupported provider: {provider}")

    def _generate_openai(self, prompt: str, model: str, temperature: float) -> str:
        client = self._openai_client()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def _generate_deepseek(self, prompt: str, model: str, temperature: float) -> str:
        client = self._deepseek_client()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def _generate_gemini(self, prompt: str, model: str, temperature: float) -> str:
        """
        Gemini uses Google's Generative Language REST API.
        This implementation avoids adding an extra SDK dependency.
        """
        if not self.gemini_api_key:
            raise LLMProviderError("GEMINI_API_KEY is missing")

        import requests

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.gemini_api_key}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature
            }
        }
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts)
