"""테스트 환경에서 settings 로드 시 필요한 환경변수를 미리 주입.

config.get_settings()가 import 시점에 호출되므로 conftest 최상단에서 설정.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
