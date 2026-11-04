from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://temi:temi@db:5432/temi"
    jwt_secret: str = "dev-secret-change-me"
    jwt_ttl_hours: int = 24 * 7
    # 기기 토큰. 토큰으로 어떤 기기인지 구분한다.
    temi_token: str = "dev-temi-token"
    pi_token: str = "dev-pi-token"
    cors_origins: str = "*"
    timezone: str = "Asia/Seoul"
    say_done_timeout_s: float = 20.0
    ready_timeout_s: float = 10.0
    ready_prompts: int = 2
    lead_in_s: float = 2.0
    # 배포 시 docker build 인자로 넣는다(scripts/deploy.sh). /api/health에서 어떤 커밋이 돌고 있는지 확인한다.
    git_commit: str = "unknown"
    # 비어 있으면 LLM을 부르지 않고 규칙 응답만 쓴다(app/llm.py).
    anthropic_api_key: str = ""
    llm_model_fast: str = "claude-haiku-5-5"
    llm_model_smart: str = "claude-sonnet-5-5"
    llm_timeout_s: float = 10.0


settings = Settings()
