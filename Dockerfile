FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 시스템 의존성 (psycopg/asyncpg 빌드용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치 (빌드 캐시 활용)
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 애플리케이션 코드 복사
COPY app ./app
COPY migrations ./migrations

# 비-root 사용자로 실행
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 기본은 API 서버로 기동 (워커는 docker-compose에서 command override)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
