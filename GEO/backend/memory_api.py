"""
Memory API layer for GEO Growth OS.
"""

from pydantic import BaseModel

try:
    from backend.vector_store import GEOVectorStore
except ImportError:
    from vector_store import GEOVectorStore


class MemoryWriteRequest(BaseModel):
    memory_id: str
    text: str
    metadata: dict | None = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


store = GEOVectorStore()


def write_memory(request: MemoryWriteRequest):
    store.add_memory(
        memory_id=request.memory_id,
        text=request.text,
        metadata=request.metadata,
    )

    return {
        "status": "success",
        "memory_id": request.memory_id,
    }


def search_memory(request: MemorySearchRequest):
    result = store.search(
        query=request.query,
        top_k=request.top_k,
    )

    return {
        "query": request.query,
        "results": result,
    }
