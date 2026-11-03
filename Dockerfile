FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 review
USER review
EXPOSE 8080
CMD ["uvicorn", "agent_review_engine.app:app", "--host", "0.0.0.0", "--port", "8080"]
