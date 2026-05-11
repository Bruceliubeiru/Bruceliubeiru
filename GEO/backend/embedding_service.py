"""
Embedding Service for GEO Growth OS.
"""

import os
from openai import OpenAI


class EmbeddingService:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv('OPENAI_API_KEY')
        )

    def create_embedding(
        self,
        text: str,
        model: str = 'text-embedding-3-small'
    ):
        response = self.client.embeddings.create(
            model=model,
            input=text,
        )

        return response.data[0].embedding
