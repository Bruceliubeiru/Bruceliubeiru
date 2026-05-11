"""
OpenAI Client for GEO Growth OS
"""

import os
from openai import OpenAI


class GEOOpenAIClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY")
        )

    def rewrite_for_geo(self, content: str):
        prompt = f"""
Rewrite the following content for GEO.

Goal:
- easier for AI systems to understand
- easier to quote
- easier to compare
- improve FAQ structure
- improve citation readiness

Content:
{content}
"""

        response = self.client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return response.choices[0].message.content
