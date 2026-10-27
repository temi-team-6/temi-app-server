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


settings = Settings()
