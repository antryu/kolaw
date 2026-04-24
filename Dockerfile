FROM python:3.12-slim

WORKDIR /app

# System deps for sentence-transformers and chromadb
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 8100

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8100"]
