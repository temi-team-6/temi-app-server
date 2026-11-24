# 서버 연동 규약 (1단계)

Temi 앱, 라즈베리파이(+Arduino), 보호자·요양사 앱이 서버와 주고받는 형식을 정한다. 검사 중 판단과 Temi 대사는 **AI(Claude)**가 맡는다: 판단 지점마다 허용된 행동 하나와 할 말을 고른다(`app/agent/ai.py`). 서버는 가드레일을 강제한다: 안전 신호·영상통화, 측정 유효성·기준선 비교, 보호자에게 물을지는 **규칙**이고, 측정 지시와 출발 신호는 고정 문장이다. AI가 꺼져 있거나(`AGENT_AI_ENABLED=false`) 실패·시간 초과이거나 가드레일을 넘으면 그 자리는 규칙대로 진행한다. 보호자 챗봇과 결과 해설도 Claude가 쓴다. 판단 방식이 바뀌어도 이 문서의 메시지 형식은 그대로 유지한다.

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
| `temi.say` | `{text, audio_url?}` | 말하고 화면에 `text`를 표시한다. `audio_url`이 있으면 그 음성 파일을 재생하고, 없으면 Temi 내장 TTS로 말한다(아래 "Temi 음성"). 받은 순서대로 이어서 말하고, 앞 문장을 끊지 않는다. 다 말하면 `temi.say_done`을 보낸다 |
| `temi.ask` | `{text, audio_url?}` | 질문을 `temi.say`와 같은 방식으로 말한다. 대답은 참가자 발화로 온다: 기본은 Pi 마이크(`speech.heard`, Pi가 보냄). Temi 앱의 음성 입력을 'Temi 내장'으로 두면 Temi가 듣고 `reply_to`를 붙여 보낸다. 서버는 `reply_to`가 아니라 지금 단계(통증 확인 중)로 대답을 가른다 |
| `temi.show` | `{screen, ...}` | 화면 전환. `screen`: `idle`, `walk`(`condition`, `attempt`), `game`(`game_id`, `game_type`, `level`). `game_type`이 `stroop`·`memory`면 Temi가 화면 터치로 게임을 진행하고 `game.completed`를 보낸다. `whack`은 안내 화면만 띄운다(진행은 Pi) |
| `temi.goto` | `{location}` | 저장된 위치로 이동하고 도착하면 `temi.arrived`를 보낸다 |
| `temi.video_call` | `{staff_id, name}` | 해당 요양사에게 영상통화를 건다. 결과는 `video_call.result`로 보낸다 |

#### Temi 음성

Temi 내장 TTS가 딱딱해서, 고정 대사는 Google Gemini TTS(Leda 목소리, 친근한 말투)로 미리 만든 파일을 쓴다.

- 서버는 미리 만든 문장이면 `audio_url`(예: `/voice/1a2b3c4d5e6f.mp3`)을 붙인다. 서버 주소 뒤에 붙여 받는다(`https://<서버>/voice/...`). 인증은 필요 없다.
- 전체 목록은 `GET /voice/manifest.json`(`{문장: 파일 이름}`)이다. 출발 신호가 늦어지지 않도록 Temi 앱은 연결할 때 파일을 미리 받아 두는 것을 권장한다.
- 지금 Temi 앱(`temi-robot-app`)은 설정의 '서버 음성' 모드에서 `GET /twin/voice/manifest.json`으로 목록을 받아 `text`로 파일을 찾는다(`audio_url`은 아직 안 씀). 이 예전 주소도 같은 파일을 계속 제공하므로 Temi 앱을 바꾸지 않아도 Leda 목소리가 나온다. 기본은 'Temi 음성'이라 시연 때는 '서버 음성'으로 바꾼다.
- 재생이 끝나면 `temi.say_done`을 보낸다(내장 TTS의 완료 콜백과 같은 시점). 받기나 재생에 실패하면 `text`를 내장 TTS로 말하고 똑같이 `temi.say_done`을 보낸다.
- 새로 등록한 어르신 이름 인사처럼 목록에 없는 문장은 `audio_url` 없이 온다. 파일은 `scripts/gen_temi_voice.py`로 만들고, 대사를 바꾸면 다시 실행해 커밋한다.

`temi.goto`는 형식만 정해 두었다. 에이전트는 아직 보내지 않는다(보행 구간 시범 이동에서 쓸 예정).

### Temi → 서버

| type | payload | 언제 |
|---|---|---|
| `temi.say_done` | `{}` | `temi.say` 하나를 다 말했을 때(SDK의 TTS 완료 콜백). `reply_to`에 그 `temi.say`의 `id`를 넣는다. **필수**: 서버는 이걸 받고 나서 출발 신호를 보낸다 |
| `speech.heard` | `{text}` | Temi 앱의 음성 입력이 'Temi 내장'일 때만. 참가자 발화를 인식했을 때. `temi.ask`의 답이면 `reply_to`를 붙인다. 기본 설정(Pi 마이크)에서는 Pi가 보낸다 |
| `temi.arrived` | `{location}` | `temi.goto` 도착 |
| `temi.goto_failed` | `{location, reason?}` | 이동이 중단됨(SDK 이동 상태 `abort`). `reason`에 SDK가 준 설명(장애물 등) |
| `video_call.result` | `{staff_id, answered}` | 통화 연결 성공(`true`) 또는 무응답·거절(`false`) |
| `game.completed` | `{game_id, rt_ms[], correct[]}` | Temi 터치 게임(스트룹, 그림카드)이 끝났을 때. 반응시간은 자극을 띄운 시각부터 터치까지 Temi가 잔다 |

### 서버 → Pi

| type | payload | Pi가 할 일 |
|---|---|---|
| `trial.start` | `{trial_id, condition, attempt}` | 측정을 준비한다. 시작선 앞에 사람이 보이면 `trial.ready`를 보낸다. 아직 재지 않는다 |
| `trial.go` | `{trial_id}` | 출발 신호가 나갔다. 이때부터 시작선·끝선 통과를 재고 끝선을 넘으면 `trial.completed`를 보낸다. 15초 안에 끝선을 넘지 않으면 `speed_mps: null`로 보낸다 |
| `trial.abort` | `{trial_id, reason}` | 측정을 취소한다. 결과를 보내지 않는다 |
| `game.start` | `{game_id, game_type, level}` | LED 버튼 패드 게임을 시작한다(`whack`만) |
| `game.abort` | `{}` | 게임을 중단한다 |

### Pi → 서버

| type | payload | 언제 |
|---|---|---|
| `rfid.scanned` | `{tag}` | 카드가 찍혔을 때. 서버가 검사를 시작한다 |
| `trial.ready` | `{trial_id}` | `trial.start` 후 시작선 앞에 사람이 한 명 보일 때. 한 번만 보내면 된다 |
| `trial.progress` | `{trial_id, position_m}` | 선택. 걷는 동안 시작선에서부터 간 거리(m, 0~4)를 초당 몇 번 보낸다. 트윈 지도에만 쓰고 판정에는 쓰지 않는다. 발목 x 픽셀을 시작선·끝선 픽셀 사이로 비례시키면 된다 |
| `trial.completed` | 아래 표 | 측정이 끝났을 때. 측정 실패도 보낸다(`speed_mps: null`) |
| `game.completed` | `{game_id, rt_ms[], correct[], raw?}` | 버튼 패드 게임(두더지 잡기)이 끝났을 때. 두 배열 길이는 같아야 한다 |
| `safety.alert` | `{kind, detail?}` | 주저앉음 등 안전 신호. `kind`: `fall` |
| `speech.heard` | `{text}` | 검사가 진행 중일 때 Pi 마이크로 들은 참가자 발화를 받아 적은 글자. 말한 덩어리마다 하나. Pi가 Temi 위에 있어 Temi 목소리도 들어가므로, 서버는 최근 15초 안에 Temi가 한 문장과 거의 같은 글자(글자만 비교해 60% 이상 같거나, 4글자 이상이 그 문장에 그대로 들어 있음)는 버린다. Pi는 걸러 보내지 않아도 된다. **Pi가 직접 받아 적지 않을 때는 녹음을 `POST /api/speech`로 올린다**(아래 "음성 업로드") |

#### 음성 업로드 (Pi는 녹음만, 받아 적기는 서버)

걸음 측정(YOLO-Pose)에 부담을 주지 않도록 Pi는 녹음만 하고, 서버가 Google Gemini로 들리는 그대로 받아 적는다. 녹음 원본은 서버에 저장하지 않는다(받아 적은 글만 남긴다). 서버 `.env`에 `GEMINI_API_KEY`가 있어야 하고, 없으면 두 API 모두 503이다.

| API | 언제 | 서버가 하는 일 |
|---|---|---|
| `POST /api/trials/{trial_id}/speech` | 이중과제 걷기 한 번이 끝났을 때. `trial.go`부터 끝선까지의 녹음 | 받아 적고 단어 수·오류 수를 세어 그 trial에 저장. 응답 `{trial_id, transcript, word_count, error_count, model}` |
| `POST /api/speech` | 검사 중 참가자가 짧게 말했을 때(통증 질문의 대답 등). 말한 덩어리마다 하나. 이중과제로 걷는 동안의 말은 위 걷기 녹음에 들어가므로 따로 보내지 않는다 | 받아 적어 `speech.heard`와 똑같이 처리(Temi 목소리 거르기 포함). 응답 `{text, model}` |

- 인증: `Authorization: Bearer <Pi 토큰>`. 본문은 녹음 파일 그대로(멀티파트 아님), `Content-Type`에 형식(`audio/wav`, `audio/mp4`(m4a), `audio/mpeg`). 10MB까지.
- 이중과제 걷기: Pi는 `trial.completed`에 **`speech_upload: true`**를 넣고 `word_count`는 **비워(null) 보내고** 녹음을 올린다. 서버는 받아 적기가 끝날 때까지 최대 20초(`STT_WAIT_S`) 판정을 기다리고, 그 뒤 `word_count` < 3이면 `task_not_engaged`로 다시 걷게 한다. 업로드와 `trial.completed`는 어느 쪽이 먼저 와도 된다. 20초 안에 못 받아 적으면 단어 수 없이 판정한다.
- `speech_upload`가 없거나 false면(녹음을 올리지 않는 지금의 Pi) 기다리지 않고 지금처럼 판정한다. 그래서 이 기능을 머지해도 Pi를 바꾸기 전까지는 동작이 그대로다. Pi가 `word_count`를 채워 보내도 그 값을 쓰고 기다리지 않는다(트윈의 가상 Pi가 이 방식).
- 채점: 동물은 서로 다른 동물 이름 수(반복은 한 번, '개·소·말' 같은 한 글자는 따로 떨어진 단어일 때만). 숫자 세기는 말한 숫자 개수가 `word_count`, 바로 앞에 말한 숫자에서 3을 뺀 값이 아닌 숫자 수가 `error_count`(첫 숫자는 100이나 97이면 맞음). 받아 적은 글은 검사 상세의 `transcript`로 볼 수 있다.

`trial.completed` payload:

| 필드 | 타입 | 설명 |
|---|---|---|
| `trial_id` | int | `trial.start`로 받은 ID |
| `speed_mps` | float \| null | 시작선~끝선 평균 속도(m/s). 측정 실패면 null |
| `duration_s` | float \| null | 통과 시간(초) |
| `stopped` | bool | 구간 안에서 멈춤 감지 |
| `max_persons` | int | 측정 중 화면 안 최대 인원 |
| `min_confidence` | float \| null | 측정 중 관절점 신뢰도 최솟값(0~1) |
| `word_count` | int \| null | 이중과제 발화 단어 수. 서버가 녹음을 받아 적어 셀 때는 null로 보낸다(위 "음성 업로드") |
| `speech_upload` | bool | 이 걷기의 녹음을 서버에 올린다. true면 서버가 받아 적을 때까지(최대 20초) 판정을 기다린다. 기본 false |
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
| `request.updated` | 요청 객체 | 누군가 답했다. 보내는 대상은 `request.created`와 같다 |
| `observation.created` | `{resident_id, observation_id}` | 관찰 기록이 추가됐다(요양사 메모, 보호자 답변, 에이전트 기록) |
| `session.ended` | `{resident_id, session_id, status, end_reason}` | 검사가 끝났다. `status`: `completed`, `aborted` |

`observation.created`와 `session.ended`는 그 어르신을 볼 수 있는 연결(요양사 전체, 연결된 보호자)에만 간다. id만 담기므로, 앱은 이벤트를 받으면 REST로 다시 조회한다.

요청 객체는 `GET /api/requests`의 항목과 같다.

```json
{
  "id": 3, "resident_id": 2, "session_id": 19,
  "target_role": "guardian", "kind": "question", "priority": "normal",
  "topic": "condition_check",
  "text": "B님의 오늘 걷기 검사 결과가 평소와 조금 달랐습니다. 이번 주 수면이나 컨디션에 변화가 있었나요?",
  "explanation": "오늘 검사에서 이중과제 비용이 ... 두 과제 모두 개인 기준보다 높았습니다. ... 진단이 아니며 ...",
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
| `explanation` | 판단 근거를 보호자에게 설명하는 문장. 없으면 `null`. 지금은 `condition_check` 질문에만 붙는다 |

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
                   세 조건 완료 → 이중과제 비용 계산 → 기준선 비교(확인 후 판단, 아래)
                   일관된 초과 → 보호자에게 request.created(condition_check, 해설 포함)
               ├─ temi.say (마무리) ─────▶
               └─ temi.show idle ────────▶
```

출발 동기화 원칙:

- 속도는 Pi가 시작선·끝선 통과 시각으로 재므로, Temi와 Pi의 시계를 맞출 필요는 없다. 동기화는 "Pi가 준비되고 안내가 끝난 뒤에 출발"이라는 순서만 보장한다.
- 이중과제는 말하기가 이미 진행 중일 때 출발해야 한다. 4m 구간은 3~4초라, 말을 꺼내는 동안 걸으면 비용이 작게 나온다.
- Temi가 `temi.say_done`을 20초 안에 보내지 않으면 서버는 경고를 남기고 진행한다.
- 대기 시간(`READY_TIMEOUT_S`, `LEAD_IN_S` 등)은 `.env`로 바꿀 수 있다.

확인 후 판단(규칙):

- 측정한 이중과제 조건(동물 이름, 숫자 거꾸로 세기)이 **모두** 개인 기준(평균 + 2SD)을 넘어야 변화 신호로 보고 보호자에게 물는다.
- 한 조건만 넘으면 Temi가 "한 번 더 확인해 볼게요"라고 하고 그 조건을 한 번 더 쟴다(`trial.start`의 `attempt`가 하나 늘어난다). 그래도 두 조건이 일치하지 않으면 관찰 기록만 남기고 묻지 않는다. 재측정에서 기준 안으로 돌아오면 일시적 차이로 기록한다.
- 같은 시간대 기록이 3회 이상이면 그것만 비교한다. 부족한데 평소 시간대와 다르면 질문과 해설에 그 사실을 적는다.
- 해설 문장은 규칙으로 만든 초안을 Claude가 다듬는다. 실패하면 초안을 그대로 쓴다.

검사 중 AI 판단 지점: 인사, 무효 측정 뒤 다시 잴지·건너뛸지(최대 3회는 서버가 막는다), 확인 재측정 안내, 마무리, 발화 처리, 통증 정도, 그만할지 되묻기, 게임 마무리. 판단과 그 이유는 트윈 상태판에 `ai.decision`으로 보인다.

검사 중 발화(`speech.heard`) 처리. AI가 지금 단계와 오늘 대화를 보고 무시·대답·측정 다시·통증 확인·그만할지 되묻기·종료 중에서 고른다. Pi 마이크는 주변 소리를 계속 듣고 말을 덩어리로 끊어 보내므로, AI 판단은 기기 연결 밖에서 돌고(안전 신호·측정 결과가 밀리지 않게) 판단하는 동안 온 말은 모아 다음 판단에 넘긴다. 안전 신호가 오거나 검사가 끝나면 진행 중인 발화 판단은 버린다. 가드레일: 이중과제 측정 중에는 AI에게 묻지 않는다(말하기가 과제다). 통증 키워드는 AI가 넘길 수 없고, 심한 통증 키워드는 AI가 낮출 수 없다. AI 판단이 없을 때의 규칙(키워드 먼저, 놓친 말만 Claude 분류):

| 발화 | 처리 |
|---|---|
| 질문("~돼요?") | 답한 뒤 진행 중인 측정을 취소하고 다시 한다 |
| 통증("아파") | 측정 취소 → `temi.ask "어디가 얼마나 아프세요?"` |
| 통증 답이 심함("일어설 수가 없어") | 요양사 긴급 알림 + 영상통화(우선순위 순). 아무도 안 받으면 알림을 한 번 더 보낸다 |
| 통증 답이 가벼움 | 요양사 확인 요청 + 스트룹 게임으로 전환 |
| 거부("하기 싫어") | 검사를 끝내고 요양사에게 확인 요청(`topic: refused`)을 보낸다. AI는 먼저 그만할지 되물을 수 있다(답에 따라 멈춘 걷기를 다시 잰다). 거부로 끝낼 때는 항상 요양사에게 알린다 |

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
| POST | `/api/chat` | 보호자 챗봇. `{resident_id, message}` → `{reply, generated_at, mock}`. Claude가 답하면 `mock: false`, 실패해 고정 응답이면 `mock: true`. 질문은 관찰 기록에도 남는다 |
| GET | `/api/residents/{id}/chat` | 내 계정의 챗봇 대화(오래된 순). 항목 `{id, role: user\|assistant, text, mock, created_at}`. 다른 계정의 대화는 보이지 않는다 |
| POST | `/api/trials/{id}/speech` | Pi 전용. 이중과제 걷기 녹음 업로드 → 받아 적기·채점(4장 "음성 업로드") |
| POST | `/api/speech` | Pi 전용. 검사 중 짧은 말 녹음 업로드 → 받아 적어 `speech.heard`로 처리 |
| POST | `/api/dev/sessions/start` | 개발용. RFID 없이 검사 시작. 요양사 계정만(보호자는 403) |
| GET | `/api/health` | 연결된 기기 목록, 배포된 커밋(`commit`) |

## 7. 데이터 의미

| 항목 | 의미 |
|---|---|
| `condition` | `single`(그냥 걷기), `animals`(동물 이름 말하며 걷기), `countdown`(숫자 거꾸로 세며 걷기) |
| `game_type` | `memory`(그림카드, 터치), `stroop`(스트룹, Temi 화면 터치), `whack`(두더지, 버튼 패드) |
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
| 녹음 보관·개인정보 | 녹음은 서버에 저장하지 않고 Gemini로만 보낸다. Google AI Studio 로그 저장 설정은 팀이 정한다 | 동의서·보관 기간이 정해지면 반영 |
| 영상통화 방식 | Temi 내장 통화를 가정 | 안 되면 `temi.video_call` 처리 주체가 바뀐다 |
| 앱 푸시 | 앱이 열려 있을 때 WebSocket으로만 받는다 | FCM을 쓰면 서버에 기기 토큰 등록 API를 추가한다 |
| 에이전트 | 1단계: 검사 중 판단·Temi 대사는 AI, 안전·측정 판정·보호자 질문은 규칙. AI가 쓴 대사는 미리 만든 음성이 없어 Temi 내장 TTS로 나온다 | 2단계: 어르신별 기억, 다시 하기 예약, 보호자·요양사 판단. 메시지 형식은 유지한다 |

## 10. 담당별 시작점

- **Pi + Arduino:** `/ws/device`에 Pi 토큰으로 연결한다. `rfid.scanned`, `trial.start`→`trial.ready`→`trial.go`→`trial.completed`, `game.start`→`game.completed`, `safety.alert` 순서로 붙인다. 트윈(`/twin/`)의 가상 Pi가 같은 메시지를 보내고, 상태판에서 실제 Pi가 보낸 메시지를 비교할 수 있다.
- **보호자·요양사 앱:** 로그인 → `/ws/app` 연결 → `GET /api/requests`로 요청 화면, `GET /api/residents/{id}/trend`로 리포트 화면. 트윈에서 시나리오를 돌리면 요청이 실제로 들어온다.
- **Temi 앱:** [`temi-robot-app`](https://github.com/temi-team-6/temi-robot-app) 저장소(런처 16405·안드로이드 6에 맞춰 Temi SDK 0.10.81 고정). 트윈의 가상 Temi(`app/twin/devices.py`)가 같은 역할을 한다. `temi.say`마다 TTS가 끝나면 `temi.say_done`을 보내는 것이 필수다. 실제 Temi를 붙이고 트윈에서 가상 Pi로 보행 시나리오를 돌리면 Temi만 따로 검증할 수 있다.
