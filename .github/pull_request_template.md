## 무엇을, 왜

<!-- 기능 이름(예: B. 확인 후 판단)과 바뀐 동작을 적습니다. -->

## 확인

- [ ] `cd server && uv run poe check` 통과 (CI에서도 자동 실행)
- [ ] 시뮬레이터(`/sim/`)로 해당 장면 확인
- [ ] `models.py`를 바꿨으면 마이그레이션 파일 포함
- [ ] API·WebSocket 형식을 바꿨으면 `schemas.py`/`protocol.py`와 `docs/protocol.md`를 함께 수정, 앱 저장소 PR 링크: 
- [ ] 기존 API 경로·함수 이름·필드를 지우거나 바꾸지 않음 (추가만)
