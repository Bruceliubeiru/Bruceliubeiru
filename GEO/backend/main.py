from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="GEO Growth OS")


class GEOAuditRequest(BaseModel):
    content: str


@app.get("/")
def root():
    return {
        "project": "GEO Growth OS",
        "status": "running",
        "message": "AI Native Growth Operating System"
    }


@app.post("/geo/audit")
def geo_audit(request: GEOAuditRequest):
    content = request.content

    score = min(100, max(30, len(content) // 10))

    return {
        "geo_score": score,
        "recommendations": [
            "Add FAQ sections",
            "Add comparison tables",
            "Improve AI-readable definitions",
            "Add proof and metrics"
        ]
    }
