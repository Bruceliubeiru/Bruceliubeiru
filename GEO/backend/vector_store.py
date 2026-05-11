"""
Vector Memory Layer for GEO Growth OS.

Stores:
- GEO experiments
- FAQ assets
- competitor intelligence
- AI answers
- GEO insights
"""

import chromadb
from chromadb.config import Settings


class GEOVectorStore:
    def __init__(self, path: str = './geo_chroma_db'):
        self.client = chromadb.PersistentClient(
            path=path,
            settings=Settings(anonymized_telemetry=False)
        )

        self.collection = self.client.get_or_create_collection(
            name='geo_memory'
        )

    def add_memory(
        self,
        memory_id: str,
        text: str,
        metadata: dict | None = None,
    ):
        self.collection.add(
            ids=[memory_id],
            documents=[text],
            metadatas=[metadata or {}],
        )

    def search(self, query: str, top_k: int = 5):
        return self.collection.query(
            query_texts=[query],
            n_results=top_k,
        )


if __name__ == '__main__':
    store = GEOVectorStore()

    store.add_memory(
        memory_id='geo_test_1',
        text='AI systems prefer FAQ-rich structured content.',
        metadata={
            'type': 'geo_insight'
        }
    )

    result = store.search('What content works well for AI answers?')

    print(result)
