# 서버 연동 규약 (1단계)

Temi 앱, 라즈베리파이(+Arduino), 보호자·요양사 앱이 서버와 주고받는 형식을 정한다. 서버의 판단 로직은 아직 **고정 규칙으로 동작하는 임시 에이전트**다. LLM 에이전트로 바뀌어도 이 문서의 메시지 형식은 그대로 유지한다.

REST API의 상세 스키마는 서버의 `/docs`(Swagger)가 기준이다. 이 문서는 WebSocket 메시지와 결정 사항을 다룬다.

## 1. 역할 경계 (결정 사항)

| 항목 | 결정 |
|---|---|
| 포즈 추적 | **Pi에서 처리**한다. 카메라도 Pi에 연결한다. 서버로는 영상을 보내지 않고 측정 결과(속도, 품질 지표)만 보낸다 |
| RFID, LED 버튼 패드 | Arduino → Pi(시리얼) → 서버. 반응시간은 Arduino 보드 안에서 잰다 |
| 안전 감지(주저앉음) | Pi가 감지해서 `safety.alert`를 보낸다. 서버는 LLM을 거치지 않고 고정 규칙으로 처리한다 |
| Temi 앱 | 서버 명령을 SDK로 실행하는 얇은 단말이다. 판단 로직을 두지 않는다 |
| 연결 방향 | 기기와 앱이 서버로 WebSocket을 먼저 연결한다. 서버가 기기로 직접 접속하지 않는다 |
| 로봇 대수 | 1단계는 Temi 1대, Pi 1대를 가정한다. 진행 중인 검사도 한 번에 하나다 |

## 2. 접속

| 대상 | 주소 | 인증 |
|---|---|---|
| Temi | `wss://<도메인>/ws/device?token=<TEMI_TOKEN>` | 기기 토큰. 토큰으로 기기를 구분한다 |
| Pi | `wss://<도메인>/ws/device?token=<PI_TOKEN>` | 기기 토큰 |
| 앱 실시간 | `wss://<도메인>/ws/app?token=<JWT>` | `POST /api/auth/login`으로 받은 JWT |
| 앱 REST | `https://<도메인>/api/...` | `Authorization: Bearer <JWT>` |

- 토큰이 틀리면 핸드셰이크가 거부된다(HTTP 403).
- 연결되면 서버가 먼저 `hello`를 보낸다.
- 같은 기기가 다시 연결하면 이전 연결은 서버가 끊는다(close code 1012).
- **25초마다 `ping`을 보낸다.** 서버는 `pong`으로 답한다. Cloudflare Tunnel은 오래 조용한 연결을 끊을 수 있다.
- **연결이 끊기면 1~5초 간격으로 다시 연결한다.** 기기 쪽에 반드시 넣는다.
- 기기가 오프라인일 때 서버가 보낸 명령은 버려진다. 다시 보내지 않는다.

## 3. 메시지 봉투

모든 WebSocket 메시지는 아래 형식이다.

```json
{
  "type": "trial.completed",
  "id": "8f2c...",
  "ts": "2026-10-27T10:00:00Z",
  "session_id": 12,
  "reply_to": "a91b...",
  "payload": {}
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `type` | 예 | 메시지 종류 |
| `id` | 아니오 | 메시지 ID. 서버가 보낸 메시지에는 항상 있다 |
| `ts` | 아니오 | ISO 8601 시각 |
| `session_id` | 아니오 | 서버가 보낼 때 진행 중인 검사 ID를 넣는다. 기기는 생략해도 된다 |
| `reply_to` | 아니오 | 어떤 명령에 대한 응답인지 표시. `temi.ask`의 답, `temi.video_call` 결과에 넣는다 |
| `payload` | 아니오 | 종류별 내용 |

형식이 틀리면 서버가 `error`를 돌려준다: `{"type":"error","payload":{"message":"..."},"reply_to":"<보낸 id>"}`

## 4. 메시지 목록

### 서버 → Temi

| type | payload | Temi가 할 일 |
|---|---|---|
| `temi.say` | `{text}` | TTS로 말하고 화면에 표시한다. 받은 순서대로 이어서 말하고, 앞 문장을 끊지 않는다. 다 말하면 `temi.say_done`을 보낸다 |
| `temi.ask` | `{text}` | 말한 뒤 음성으로 대답을 듣는다. 대답은 `speech.heard`에 `reply_to`를 붙여 보낸다 |
| `temi.show` | `{screen, ...}` | 화면 전환. `screen`: `idle`, `walk`(`condition`, `attempt`), `game`(`game_id`, `game_type`, `level`) |
| `temi.goto` | `{location}` | 저장된 위치로 이동하고 도착하면 `temi.arrived`를 보낸다 |
| `temi.video_call` | `{staff_id, name}` | 해당 요양사에게 영상통화를 건다. 결과는 `video_call.result`로 보낸다 |

`temi.goto`는 형식만 정해 두었다. 임시 에이전트는 아직 보내지 않는다.

### Temi → 서버

| type | payload | 언제 |
|---|---|---|
| `temi.say_done` | `{}` | `temi.say` 하나를 다 말했을 때(SDK의 TTS 완료 콜백). `reply_to`에 그 `temi.say`의 `id`를 넣는다. **필수**: 서버는 이걸 받고 나서 출발 신호를 보낸다 |
| `speech.heard` | `{text}` | 참가자 발화를 인식했을 때. `temi.ask`의 답이면 `reply_to`를 붙인다 |
| `temi.arrived` | `{location}` | `temi.goto` 도착 |
| `video_call.result` | `{staff_id, answered}` | 통화 연결 성공(`true`) 또는 무응답·거절(`false`) |
| `game.completed` | `{game_id, rt_ms[], correct[]}` | 터치스크린 게임(그림카드)이 끝났을 때 |

### 서버 → Pi

| type | payload | Pi가 할 일 |
|---|---|---|
| `trial.start` | `{trial_id, condition, attempt}` | 측정을 준비한다. 시작선 앞에 사람이 보이면 `trial.ready`를 보낸다. 아직 재지 않는다 |
| `trial.go` | `{trial_id}` | 출발 신호가 나갔다. 이때부터 시작선·끝선 통과를 재고 끝선을 넘으면 `trial.completed`를 보낸다. 15초 안에 끝선을 넘지 않으면 `speed_mps: null`로 보낸다 |
| `trial.abort` | `{trial_id, reason}` | 측정을 취소한다. 결과를 보내지 않는다 |
| `game.start` | `{game_id, game_type, level}` | LED 버튼 패드 게임을 시작한다(`whack`, `stroop`) |
| `game.abort` | `{}` | 게임을 중단한다 |

### Pi → 서버

| type | payload | 언제 |
|---|---|---|
| `rfid.scanned` | `{tag}` | 카드가 찍혔을 때. 서버가 검사를 시작한다 |
| `trial.ready` | `{trial_id}` | `trial.start` 후 시작선 앞에 사람이 한 명 보일 때. 한 번만 보내면 된다 |
| `trial.completed` | 아래 표 | 측정이 끝났을 때. 측정 실패도 보낸다(`speed_mps: null`) |
| `game.completed` | `{game_id, rt_ms[], correct[], raw?}` | 버튼 패드 게임이 끝났을 때. 두 배열 길이는 같아야 한다 |
| `safety.alert` | `{kind, detail?}` | 주저앉음 등 안전 신호. `kind`: `fall` |

`trial.completed` payload:

| 필드 | 타입 | 설명 |
|---|---|---|
| `trial_id` | int | `trial.start`로 받은 ID |
| `speed_mps` | float \| null | 시작선~끝선 평균 속도(m/s). 측정 실패면 null |
| `duration_s` | float \| null | 통과 시간(초) |
| `stopped` | bool | 구간 안에서 멈춤 감지 |
| `max_persons` | int | 측정 중 화면 안 최대 인원 |
| `min_confidence` | float \| null | 측정 중 관절점 신뢰도 최솟값(0~1) |
| `word_count` | int \| null | 이중과제 발화 단어 수(9장 미정 사항 참고) |
| `error_count` | int \| null | 숫자 세기 오류 수 |
| `early_start` | bool | `trial.go` 전에 시작선을 넘었다 |
| `raw` | object | 디버깅용 자유 데이터 |

서버의 무효 판정 규칙(1단계):

| 사유 | 조건 |
|---|---|
| `early_start` | `early_start`가 true |
| `no_measurement` | `speed_mps`가 null |
| `multiple_persons` | `max_persons` ≥ 2 |
| `out_of_frame` | `min_confidence` < 0.5 |
| `stopped` | `stopped`가 true |
| `task_not_engaged` | 이중과제 조건에서 `word_count` < 3 |
| `no_person` | 서버 판정. 안내가 끝난 뒤 `trial.ready`가 오지 않음(10초씩 3번 기다리며 사이에 "시작선 앞에 서 주세요" 안내). 서버가 `trial.abort`를 보낸다 |

### 서버 → 앱 (`/ws/app`)

| type | payload | 설명 |
|---|---|---|
| `request.created` | 요청 객체 | 에이전트가 새 질문·확인 요청·알림을 만들었다. 보호자 요청은 해당 보호자에게만, 요양사 요청은 요양사 전체에게 간다 |
| `request.updated` | 요청 객체 | 누군가 답했다 |

요청 객체는 `GET /api/requests`의 항목과 같다.

```json
{
  "id": 3, "resident_id": 2, "session_id": 19,
  "target_role": "guardian", "kind": "question", "priority": "normal",
  "topic": "condition_check",
  "text": "B님의 오늘 걷기 검사 결과가 평소와 조금 달랐습니다. 이번 주 수면이나 컨디션에 변화가 있었나요?",
  "options": ["잠을 잘 못 주무셨어요", "평소와 같아요", "요즘 길을 자주 헤매세요"],
  "status": "pending", "answer": null,
  "created_at": "...", "answered_at": null
}
```

| 필드 | 값 |
|---|---|
| `kind` | `question`(보호자에게 묻기), `check`(요양사 확인 요청), `alert`(긴급 알림) |
| `priority` | `urgent`(즉시 알림), `normal`, `digest`(일일 요약) |
| `topic` | 앱이 화면을 고르는 데 쓴다. `condition_check`, `pain_check`, `emergency`, `consult` |
| `options` | 빈 배열이면 자유 입력 |

답변: `POST /api/requests/{id}/answer` `{"answer": "..."}`. 이미 답한 요청이면 409.

## 5. 보행 검사 흐름

```text
Pi            서버                        Temi
rfid.scanned ─▶ 검사 시작
               ├─ temi.say (인사) ───────▶
◀── trial.start (single)               측정 준비
               ├─ temi.show walk ────────▶
               ├─ temi.say (지시) ───────▶
trial.ready ──▶                ◀── temi.say_done
               (둘 다 왔을 때만 다음으로)
               [이중과제] temi.say "지금부터 말하기를 시작하세요" → say_done → 2초 대기
               ├─ temi.say "출발하세요" ───▶
◀── trial.go                           이때부터 선 통과를 잔다
trial.completed ─▶ 유효 판정
                   유효 → 다음 조건(animals → countdown)
                   무효 → temi.say(사유별 안내) + 같은 조건 재측정(최대 3회)
...
                   세 조건 완료 → 이중과제 비용 계산 → 기준선 비교
                   기준 초과 → 보호자에게 request.created(condition_check)
               ├─ temi.say (마무리) ─────▶
               └─ temi.show idle ────────▶
```

출발 동기화 원칙:

- 속도는 Pi가 시작선·끝선 통과 시각으로 재므로, Temi와 Pi의 시계를 맞출 필요는 없다. 동기화는 "Pi가 준비되고 안내가 끝난 뒤에 출발"이라는 순서만 보장한다.
- 이중과제는 말하기가 이미 진행 중일 때 출발해야 한다. 4m 구간은 3~4초라, 말을 꺼내는 동안 걸으면 비용이 작게 나온다.
- Temi가 `temi.say_done`을 20초 안에 보내지 않으면 서버는 경고를 남기고 진행한다.
- 대기 시간(`READY_TIMEOUT_S`, `LEAD_IN_S` 등)은 `.env`로 바꿀 수 있다.

검사 중 발화(`speech.heard`)에 대한 임시 규칙:

| 발화 | 처리 |
|---|---|
| 질문("~돼요?") | 답한 뒤 진행 중인 측정을 취소하고 다시 한다 |
| 통증("아파") | 측정 취소 → `temi.ask "어디가 얼마나 아프세요?"` |
| 통증 답이 심함("일어설 수가 없어") | 요양사 긴급 알림 + 영상통화(우선순위 순). 아무도 안 받으면 알림을 한 번 더 보낸다 |
| 통증 답이 가벼움 | 요양사 확인 요청 + 스트룹 게임으로 전환 |
| 거부("하기 싫어") | 검사를 끝낸다 |

`safety.alert`는 발화와 관계없이 즉시 측정 취소, 요양사 긴급 알림, 영상통화로 이어진다.

## 6. REST API 요약

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → `{access_token, user}` |
| GET | `/api/me` | 내 정보 |
| GET | `/api/residents` | 보호자는 자기 어르신만, 요양사는 전체 |
| GET | `/api/residents/{id}` | 어르신 정보 |
| GET | `/api/residents/{id}/trend` | 검사별 요약(이중과제 비용, 시간대, 게임 지표, `synthetic`). 리포트 그래프용 |
| GET | `/api/residents/{id}/sessions/{sid}` | 검사 상세(측정 시도별 결과) |
| GET/POST | `/api/residents/{id}/observations` | 관찰 기록 조회, 메모 추가 |
| GET | `/api/requests?status=pending` | 나에게 온 요청 |
| POST | `/api/requests/{id}/answer` | 요청에 답하기 |
| POST | `/api/chat` | 보호자 챗봇. 1단계는 고정 응답(`mock: true`) |
| POST | `/api/dev/sessions/start` | 개발용. RFID 없이 검사 시작 |
| GET | `/api/health` | 연결된 기기 목록 |

## 7. 데이터 의미

| 항목 | 의미 |
|---|---|
| `condition` | `single`(그냥 걷기), `animals`(동물 이름 말하며 걷기), `countdown`(숫자 거꾸로 세며 걷기) |
| `game_type` | `memory`(그림카드, 터치), `stroop`(스트룹, 화면+버튼 패드), `whack`(두더지, 버튼 패드) |
| `dual_task_cost` | `(single 속도 − 이중과제 속도) / single 속도 × 100`. 조건별 마지막 유효 측정 기준 |
| `synthetic` | 시연용 가상 기록이다. **앱 화면에 반드시 "가상 데이터"로 표시한다** |
| `time_of_day` | `morning`, `afternoon`, `evening`(서버 시간대 Asia/Seoul) |
| `call_priority` | 요양사 영상통화 연결 순서. 1이 먼저 |

기준선 비교(1단계): 같은 시간대 기록이 3회 이상이면 그 기록만, 아니면 전체 기록으로 평균 + 2SD를 넘는지 본다. 연습 효과 보정은 아직 없다.

## 8. 개발용 계정과 시드 데이터

서버가 처음 뜰 때 DB가 비어 있으면 자동으로 넣는다. 비밀번호는 모두 `temi1234`이다.

| 계정 | 역할 | 비고 |
|---|---|---|
| `guardian_a` | 보호자 | A의 보호자 |
| `guardian_b` | 보호자 | B의 보호자 |
| `staff1` | 요양사 | 영상통화 1순위 |
| `staff2` | 요양사 | 영상통화 2순위 |

| 어르신 | RFID 태그 |
|---|---|
| A | `RFID-A-0001` |
| B | `RFID-B-0002` |

A·B 모두 지난 3주 동안 월·수·금 오전에 검사한 가상 기록이 9회씩 있다.

## 9. 미정 사항

| 항목 | 지금 상태 | 정해지면 바뀌는 것 |
|---|---|---|
| 이중과제 단어 수 출처 | Pi가 `trial.completed.word_count`로 보낸다고 가정 | 음성 인식을 Temi나 서버에서 하면 서버가 합친다 |
| 음성 업로드 | 없음 | 외부 마이크와 서버 STT를 쓰면 업로드 API를 추가한다 |
| 영상통화 방식 | Temi 내장 통화를 가정 | 안 되면 `temi.video_call` 처리 주체가 바뀐다 |
| 앱 푸시 | 앱이 열려 있을 때 WebSocket으로만 받는다 | FCM을 쓰면 서버에 기기 토큰 등록 API를 추가한다 |
| 에이전트 | 고정 규칙 | LLM 에이전트로 교체. 메시지 형식은 유지한다 |

## 10. 담당별 시작점

- **Pi + Arduino:** `/ws/device`에 Pi 토큰으로 연결한다. `rfid.scanned`, `trial.start`→`trial.ready`→`trial.go`→`trial.completed`, `game.start`→`game.completed`, `safety.alert` 순서로 붙인다. 시뮬레이터의 Pi 패널이 같은 메시지를 보내니 형식을 비교할 수 있다.
- **보호자·요양사 앱:** 로그인 → `/ws/app` 연결 → `GET /api/requests`로 요청 화면, `GET /api/residents/{id}/trend`로 리포트 화면. 시뮬레이터로 검사를 돌리면 요청이 실제로 들어온다.
- **Temi 앱:** 시뮬레이터의 Temi 패널이 Temi 앱과 같은 역할을 한다. `temi.say`마다 TTS가 끝나면 `temi.say_done`을 보내는 것이 필수다. 실제 앱은 이 패널의 동작을 Temi SDK 호출로 바꾸면 된다.
