from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import READ_ONLY_MESSAGE, read_only_enabled
from .pages import router as pages_router
from .services import DomainError

app = FastAPI(title="CheckStock", version="0.1.0")
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

# Куда вернуть после отклонённой записи, если браузер не прислал Referer.
READ_ONLY_FALLBACK = (
    ("/orders", "/orders"),
    ("/warehouses", "/warehouses"),
    ("/transfers", "/warehouses"),
    ("/receipts", "/"),
    ("/api/", "/"),
)


def _return_path(request: Request) -> str:
    """Исходная страница: Referer того же хоста, иначе — по префиксу пути."""
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        host = request.headers.get("host", "")
        if not parts.netloc or parts.netloc == host:
            return parts.path or "/"
    path = request.url.path
    for prefix, target in READ_ONLY_FALLBACK:
        if path.startswith(prefix):
            return target
    return "/"


@app.middleware("http")
async def demo_read_only(request: Request, call_next):
    # Отсекаем запись до маршрутизации: ни один обработчик не выполнится,
    # в БД ничего не попадёт. Читателю возвращаем понятное сообщение.
    if read_only_enabled() and request.method not in ("GET", "HEAD", "OPTIONS"):
        target = _return_path(request)
        separator = "&" if "?" in target else "?"
        query = urlencode({"msg": READ_ONLY_MESSAGE, "level": "info"})
        return RedirectResponse(f"{target}{separator}{query}", status_code=303)
    return await call_next(request)


app.include_router(pages_router)
app.include_router(api_router)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})


@app.get("/health")
def health():
    return {"status": "ok"}
