"""
Retrieval Engine for GEO Growth OS.

Combines vector memory with semantic GEO retrieval.
"""

from vector_store import GEOVectorStore


class GEORetrievalEngine:
    def __init__(self):
        self.store = GEOVectorStore()

    def retrieve_geo_knowledge(
        self,
        query: str,
        top_k: int = 5,
    ):
        result = self.store.search(
            query=query,
            top_k=top_k,
        )

        return {
            'query': query,
            'results': result,
        }


if __name__ == '__main__':
    engine = GEORetrievalEngine()

    result = engine.retrieve_geo_knowledge(
        query='How to improve AI citation visibility?'
    )

    print(result)
