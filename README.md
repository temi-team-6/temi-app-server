# Temi 이중과제 보행 검사 서버

FastAPI + PostgreSQL. 기기·앱 연동 규약은 [docs/protocol.md](docs/protocol.md)를 본다.

## 실행

```bash
cp .env.example .env   # POSTGRES_PASSWORD, JWT_SECRET 값을 바꾼다
docker compose up -d --build
```

- API 문서: http://localhost:8000/docs
- 트윈(가상 기기·시나리오·상태판): http://localhost:8000/twin/
- 상태: http://localhost:8000/api/health

api 컨테이너는 뜨기 전에 `alembic upgrade head`로 DB 스키마를 최신으로 맞춘다. 그다음 DB가 비어 있으면 개발용 계정과 시연용 가상 기록을 넣는다(계정은 protocol.md 8장).

DB를 처음 상태로 되돌리기(모든 데이터 삭제 → 마이그레이션 → 시드):

```bash
docker compose exec api python -m app.seed --reset
```

## 함께 작업하기

기능 단위로 나눠 여러 명이 같은 서버를 고친다. 명령은 Windows·macOS 모두 같다(`uv`, Docker 필요).

```bash
cd server
uv sync                 # 개발 도구까지 설치
uv run poe db-test      # 테스트용 Postgres(127.0.0.1:55432, 메모리에만 저장) 띄우기
uv run poe check        # PR 전에: 린트 + 타입 검사 + 테스트
uv run poe             # 명령 목록
```

- **브랜치:** `main`은 운영 배포본이다. 기능마다 브랜치를 만들고 PR로 합친다. PR마다 GitHub Actions가 `poe check`와 같은 검사를 돌린다.
- **에이전트는 흐름별 파일**이다(`app/agent/`). 자기 기능의 흐름 파일만 고치고, 공통 도구(`core.py`)를 바꾸면 PR에 적는다.
- **테스트:** `tests/fakes.py`의 `FakeDevices`가 Temi·Pi를, `connect_app`이 앱 연결을 흉내 낸다. 새 흐름을 넣으면 `tests/test_walk_flow.py`처럼 시나리오 테스트를 하나 추가한다.
- **API·메시지 형식(계약):** 바꾸면 `schemas.py`/`protocol.py`와 `docs/protocol.md`를 같은 PR에서 고친다. 기존 경로·필드는 지우거나 바꾸지 않고 추가만 한다. 앱이 아는 필드가 사라지면 앱이 깨진다.
- **LLM:** Claude 호출은 `app/llm.py`의 `complete()`만 쓴다. `None`이 오면(키 없음·실패·시간 초과) 기존 규칙 응답으로 진행한다. 안전 신호 처리에는 쓰지 않는다. 테스트는 키 없이 돈다.

## 계정·어르신 관리

공개 회원가입은 없다. 운영자가 서버에서 명령으로 만든다. 어르신 한 명에 보호자는 한 명이고, 다시 연결하면 보호자가 바뀐다.

```bash
docker compose exec api python -m app.manage list
docker compose exec api python -m app.manage create-resident --name C --rfid RFID-C-0003 --birth-year 1950
docker compose exec api python -m app.manage create-user --username guardian_c --name "C 보호자" --role guardian --password <8자 이상> --resident-id 3
docker compose exec api python -m app.manage create-user --username staff3 --name "요양사 박" --role staff --password <8자 이상> --call-priority 3
docker compose exec api python -m app.manage link-guardian --resident-id 3 --guardian guardian_c
```

## 배포

운영 VM에서 `scripts/deploy.sh`를 실행한다. `main`을 받아 다시 빌드하고, `/api/health`의 `commit`이 새 커밋으로 바뀐 것을 확인한다. 지금 배포된 커밋은 `curl https://temi.hylink.kr/api/health`로 본다.

## DB 스키마 변경 (Alembic)

모델(`server/app/models.py`)을 바꾸면 마이그레이션 파일을 만들어 함께 커밋한다. 배포하면 api가 뜨면서 자동으로 적용하고, 기존 데이터는 유지된다.

```bash
docker compose exec api alembic revision --autogenerate -m "add something"
docker compose cp api:/app/migrations/versions/. server/migrations/versions/   # 생성된 파일을 저장소로 가져오기
```

Docker로 api를 띄우지 않았다면 테스트 DB로 만들 수도 있다.

```bash
cd server
uv run poe db-test
DATABASE_URL=postgresql+asyncpg://temi:temi@localhost:55432/temi_test uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://temi:temi@localhost:55432/temi_test uv run alembic revision --autogenerate -m "add something"
```

여럿이 동시에 작업할 때:

- 두 사람이 각자 마이그레이션을 만들면 head가 둘이 된다. 테스트(`test_single_migration_head`)가 실패하면 `main`을 받은 뒤 `uv run alembic merge heads -m "merge"`로 합치고 커밋한다.
- `models.py`만 바꾸고 마이그레이션을 빠뜨리면 테스트(`test_models_match_migrations`)가 실패한다.
- 이미 `main`에 들어간 마이그레이션 파일은 고치지 않는다. 바꿀 것이 있으면 새 마이그레이션을 만든다(운영 DB는 이미 적용했다).

- 생성된 파일은 꼭 열어서 확인한다. 컬럼 이름 변경은 "삭제 + 추가"로 나오므로 직접 `alter_column`으로 고친다.
- 현재 버전 확인: `docker compose exec api alembic current`
- enum 값 추가(`topic`, `condition` 등)는 문자열 컬럼이라 마이그레이션이 필요 없다.
- 마이그레이션 도입 전에 만든 DB(`alembic_version` 테이블 없음)는 api가 뜨지 못한다. 한 번만 `docker compose down -v`로 볼륨을 지우고 다시 띄운다.

## 앱 API 클라이언트 생성

`/openapi.json`의 operationId는 라우트 함수 이름(`login`, `list_requests`, `get_trend` 등)으로 고정돼 있고, 태그(`auth`, `residents`, `requests`, `chat`, `system`, `dev`)는 생성되는 API 클래스 단위다. **앱이 생성 코드를 쓰므로 기존 함수 이름과 태그는 바꾸지 않는다.** 새 API는 추가만 한다.

```bash
# 예: Flutter(dio)
npx @openapitools/openapi-generator-cli generate -i https://temi.hylink.kr/openapi.json -g dart-dio -o temi_api
# 예: Android(Kotlin)
npx @openapitools/openapi-generator-cli generate -i https://temi.hylink.kr/openapi.json -g kotlin -o temi-api
```

## Cloudflare Tunnel로 공개

1. Cloudflare Zero Trust → Networks → Tunnels에서 터널을 만들고 토큰을 `.env`의 `TUNNEL_TOKEN`에 넣는다.
2. 터널의 Public Hostname에 서비스 `http://api:8000`을 등록한다.
3. 실행:

```bash
docker compose --profile tunnel up -d
```

주의:

- API 호스트에는 Cloudflare Access를 걸지 않는다. 걸면 기기와 앱이 로그인 화면에 막힌다. 관리용 화면을 따로 만들면 그 호스트에만 건다.
- WebSocket은 터널을 통과하지만, 오래 조용하면 끊길 수 있다. 클라이언트는 25초마다 `ping`을 보내고, 끊기면 다시 연결한다.
- 공개 전에 `.env`의 `JWT_SECRET`, `TEMI_TOKEN`, `PI_TOKEN`을 바꾼다. 개발용 계정 비밀번호(`temi1234`)도 공개 주소에서는 바꿔야 한다.

## 트윈

`/twin/`은 가상 Temi·Pi, 시나리오 자동 실행, 실시간 상태판을 한 화면에 모은 것이다. 요양사 계정(`staff1`)으로 로그인한다. (`/sim/`은 여기로 넘어간다.)

- **상태판(항상 볼 수 있음):** 실습실 2D 지도(4m 구간 위 참가자 위치, Temi가 있는 저장 위치), 진행 중인 검사의 단계(조건, 시도, 기다리는 신호, 통화 순서), 기기·앱과 오간 모든 메시지, 앱으로 간 요청과 해설. 실제 기기를 붙여도 같은 화면으로 본다.
- **시나리오:** 정상 검사, 검사 방해, 기준 초과, 한 과제만 느림, 검사 중 질문, 통증(가벼움/심함), 주저앉음, 참여 거부, 보호자 답변. 버튼 하나로 끝까지 돈다. 같은 시나리오가 `tests/test_twin.py`에서도 돈다(`app/twin/scenarios.py`).
- **직접 조작:** A/B 카드, 참가자 발화(직접 입력, 미리 만든 문장), 영상통화 받음/안 받음, 게임 결과 보내기.
- **실제 기기와 섞기:** 기기별로 가상을 켜고 끈다. 실제 Temi가 붙어 있으면 가상 Temi는 붙지 않고, 보행 시나리오는 실제 Temi + 가상 Pi로 돈다. 가상 Temi가 필요한 시나리오(발화·통화)는 거부한다. 가상 기기가 붙어 있는데 실제 기기가 접속하면 실제 기기가 이긴다.
- 가상 Pi가 시작한 검사는 가상 기록(`synthetic`)으로 저장돼 앱에 "가상 데이터"로 보인다.

시나리오와 가상 기기 제어는 `.env`의 `TWIN_ENABLED=true`일 때만 된다. 시연 당일에는 `false`로 두고 상태판만 본다.

## 로컬 개발 (Docker 없이 API만)

```bash
cd server
uv sync
export DATABASE_URL=postgresql+asyncpg://temi:<비밀번호>@localhost:5432/temi
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

DB는 `docker compose up -d db`로 띄우고, 5432 포트를 열어야 한다(compose 기본값은 열지 않는다).

## 구조

```text
scripts/deploy.sh   운영 VM 배포
server/migrations/  Alembic 마이그레이션 (versions/에 변경 이력)
server/tests/       시나리오·API·마이그레이션 테스트 (fakes.py: 가짜 Temi·Pi·앱)
server/app/
  main.py      앱 생성, WebSocket 엔드포인트
  api.py       REST API
  agent/       임시 규칙 에이전트
    core.py      세션 상태, 기기·앱 전송, 관찰 기록·요청 생성
    walk.py      보행 검사 진행, 재측정, 기준선 비교
    care.py      검사 중 발화, 안전 신호, 영상통화, 게임
    guardian.py  앱 답변 처리와 보호자 답변 판단
  llm.py       Claude 호출과 분류 (실패하면 None → 규칙 응답)
  chat.py      보호자 챗봇 (추세·관찰 기록 근거, 대화 이력)
  events.py    어르신별 앱 이벤트 전송 (관찰 기록, 검사 종료)
  manage.py    운영자용 계정·어르신 관리 명령
  analysis.py  무효 판정, 이중과제 비용, 기준선 비교
  protocol.py  메시지 봉투와 payload 스키마
  models.py    DB 모델
  hub.py       WebSocket 연결 관리
  seed.py      개발용 계정·가상 기록
  twin/        트윈: 가상 기기(devices), 메시지·지도 상태(monitor), 시나리오, 제어 API
  static/twin/ 트윈 화면
```
