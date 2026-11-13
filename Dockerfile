FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
COPY requirements-dev.txt .
# Демо-стенд прогоняет scripts/verify_flow.py внутри контейнера, ему нужен httpx.
# Обычная сборка (docker-compose.yml) dev-зависимости не ставит.
ARG INSTALL_DEV=false
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# seed идемпотентен: на пустой базе загрузит тестовые данные, на заполненной — ничего не тронет
CMD ["sh", "-c", "python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
