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
    # 검사 진행 AI(app/agent/ai.py): 검사 중 판단과 Temi 대사를 Claude가 맡는다. false면 규칙 에이전트만 쓴다.
    agent_ai_enabled: bool = True
    # 판단 한 번을 기다리는 시간. 넘기면 그 판단은 규칙대로 한다(참가자가 기다리는 시간이다).
    agent_ai_timeout_s: float = 6.0
    # 음성 받아 적기(app/stt.py). 비어 있으면 받아 적지 않고 Pi가 보낸 word_count만 쓴다.
    gemini_api_key: str = ""
    gemini_stt_model: str = "gemini-3.6-flash"
    stt_timeout_s: float = 30.0
    # 이중과제 trial.completed에 word_count가 없으면 녹음 업로드·받아 적기를 이만큼 기다린 뒤 판정한다.
    stt_wait_s: float = 20.0
    # 트윈의 가상 기기·시나리오 제어(/api/twin). 상태판 보기(/ws/monitor)는 꺼도 된다. 시연 당일에는 끈다.
    twin_enabled: bool = False


settings = Settings()
