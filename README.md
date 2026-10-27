# Temi 이중과제 보행 검사 서버

FastAPI + PostgreSQL. 기기·앱 연동 규약은 [docs/protocol.md](docs/protocol.md)를 본다.

## 실행

```bash
cp .env.example .env   # POSTGRES_PASSWORD, JWT_SECRET 값을 바꾼다
docker compose up -d --build
```

- API 문서: http://localhost:8000/docs
- 시뮬레이터: http://localhost:8000/sim/
- 상태: http://localhost:8000/api/health

처음 뜰 때 DB가 비어 있으면 개발용 계정과 시연용 가상 기록을 넣는다(계정은 protocol.md 8장).

DB를 처음 상태로 되돌리기:

```bash
docker compose exec api python -m app.seed --reset
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

## 시뮬레이터

`/sim/`은 Temi, 라즈베리파이, 보호자·요양사 앱을 한 화면에서 흉내 낸다. 실제 기기 없이 전체 흐름을 돌릴 수 있다.

1. "기기 연결"을 누른다. Temi와 Pi가 서버에 붙는다.
2. 앱 패널에서 `guardian_b`로 로그인한다.
3. Pi 패널에서 "B 카드"를 누르면 검사가 시작된다.
4. Pi 패널에 "출발! 측정 중"이 뜨면(`trial.go`) "정상 완료"나 방해 버튼(멈춤, 화면 밖, 2명, 말 안 함, 신호 전에 출발)으로 결과를 보낸다. 상단의 자동 전송 체크를 끄면 `trial.ready`와 `temi.say_done`을 직접 보내며 출발 동기화를 확인할 수 있다. 이중과제 조건에서 속도를 0.88로 보내면 기준선을 넘어 보호자 질문이 생긴다.
5. Temi 패널에서 "다리가 아파", "아무 동물이나 돼요?" 같은 발화를 보내 에이전트 반응을 본다.

실제 기기를 붙일 때는 같은 기기 토큰으로 연결하면 된다. 시뮬레이터가 연결된 상태에서 실제 기기가 붙으면 이전 연결은 끊긴다.

## 로컬 개발 (Docker 없이 API만)

```bash
cd server
uv sync
DATABASE_URL=postgresql+asyncpg://temi:<비밀번호>@localhost:5432/temi uv run uvicorn app.main:app --reload
```

DB는 `docker compose up -d db`로 띄우고, 5432 포트를 열어야 한다(compose 기본값은 열지 않는다).

## 구조

```text
server/app/
  main.py      앱 생성, WebSocket 엔드포인트
  api.py       REST API
  agent.py     임시 규칙 에이전트 (검사 진행, 재측정, 요청 생성)
  analysis.py  무효 판정, 이중과제 비용, 기준선 비교
  protocol.py  메시지 봉투와 payload 스키마
  models.py    DB 모델
  hub.py       WebSocket 연결 관리
  seed.py      개발용 계정·가상 기록
  static/      시뮬레이터
```
