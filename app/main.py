from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .pages import router as pages_router
from .services import DomainError

app = FastAPI(title="CheckStock", version="0.1.0")
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
app.include_router(pages_router)
app.include_router(api_router)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})


@app.get("/health")
def health():
    return {"status": "ok"}
