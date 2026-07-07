# techtalk-2026

AWS Cloud Club Tech Talk용 레포지토리

**Discord에서 부리는 나만의 코딩 에이전트** — opencode 서버를 백엔드로, Discord 봇을 프론트로
삼아 코드 레포 작업 · 이슈 처리 · 옵시디언 볼트(제2의 뇌) 참고 · 블로그 발행까지 한 봇에서.

---

## 아키텍처

```
Discord ──> bot (discord.py) ──HTTP──> opencode serve (에이전트)
                                          ├─ /workspace  작업 레포 clone (git, gh CLI)
                                          └─ /vault       옵시디언 볼트 (D:/obsidian)
```

- **opencode 컨테이너**: 상시 HTTP 서버. 에이전트가 `git` / `gh`로 clone·브랜치·커밋·push·이슈 처리.
- **bot 컨테이너**: Discord 게이트웨이에 붙어 커맨드를 받아 opencode에 프롬프트로 전달.
- opencode 포트는 **호스트에 노출하지 않고** compose 내부 네트워크로만 봇이 접근 + basic auth 이중 방어.
- 에이전트의 파일·bash 도구 실행이 **컨테이너 안에 격리**됨.

---

## 사전 준비

### 1. `.env` 만들기

```bash
cp .env.example .env
```

`.env`에 아래 값을 채운다:

| 변수 | 설명 | 발급처 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Claude API 키 | [console.anthropic.com](https://console.anthropic.com) |
| `DISCORD_TOKEN` | 봇 토큰 | Developer Portal → Bot → Reset Token |
| `OPENCODE_PW` | opencode basic auth 비밀번호 (직접 정함) | `openssl rand -base64 24` |
| `GITHUB_TOKEN` | push / 이슈용 PAT (`repo` 스코프) | [github.com/settings/tokens](https://github.com/settings/tokens) |
| `GIT_NAME` / `GIT_EMAIL` | 커밋 작성자 | 본인 git 정보 |

### 2. Discord 봇 설정 (Developer Portal)

- **Bot 탭 → Reset Token** → `.env`의 `DISCORD_TOKEN`에 붙여넣기
- **Bot 탭 → MESSAGE CONTENT INTENT 토글 ON** ⚠️ (안 켜면 커맨드 뒤 텍스트를 못 읽어 조용히 실패)
- **OAuth2 → URL Generator** → scope `bot`, 권한 `Send Messages` + `Read Message History` → 생성된 URL로 서버에 초대
- ⚠️ **인터랙션 엔드포인트 URL은 비워둘 것** (게이트웨이 방식이라야 봇이 동작)

### 3. Docker Desktop

- Docker Desktop 실행 (Engine running 확인)
- Settings → Resources → File Sharing에 `D:\` 가 포함돼야 볼트 마운트가 됨 (WSL2 백엔드면 기본 OK)

---

## 띄우기

```bash
docker compose up --build
```

두 컨테이너(opencode + bot)가 뜬다. Discord에서 `/ping` 을 쳐서
`opencode: ✅ 연결됨` 이 나오면 성공.

종료:

```bash
docker compose down          # 컨테이너 정리 (볼륨/작업공간은 유지)
```

---

## 커맨드

| 커맨드 | 설명 | 예시 |
|--------|------|------|
| `/ping` | 봇 + opencode 서버 헬스체크 | `/ping` |
| `/repo <url>` | GitHub 레포를 `/workspace/repo`에 clone | `/repo https://github.com/Team-STORIX/STORIX-BE-2.0` |
| `/branch <이름>` | 브랜치 이동 / 생성 | `/branch fix/login` |
| `/issue <번호> [지시]` | 이슈 읽고 → 연관 브랜치 생성 → 작업 시작 | `/issue 42 로그인 타임아웃 고쳐줘` |
| `/ask <질문>` | 레포에서 작업 + 볼트 참고 (커밋·push도 여기서) | `/ask 방금 수정한 거 커밋하고 푸시해줘` |
| `/doc [제목]` | 이번 브랜치 작업을 git 기록으로 재구성해 상세 문서(.md) 생성 | `/doc 로그인 타임아웃 수정` |
| `/note <지시>` | 볼트에 내용 저장/정리 (명시적 쓰기) | `/note 오늘 배운 Docker 볼륨 개념 정리해서 저장` |
| `/blog <주제>` | 볼트 내용 바탕으로 블로그 포스트 작성 → 커밋 → main에 push | `/blog AWS RDS 트러블슈팅 정리` |

### 동작 규칙 (봇이 에이전트에 항상 주입)

- **볼트(`/vault`)**: 기본은 참고용 읽기만. `/note`처럼 명시적으로 요청할 때만 쓰기.
- **커밋**: Conventional Commits(`type(scope): 요약`), 본문에 "무엇을 왜", 이슈 있으면 `Refs/Closes #번호`,
  **에이전트 서명(Co-authored-by 등) 금지** — 작성자는 사람만.
- **문서화**: 코드 변경 시 관련 문서도 갱신, 새 함수엔 독스트링.

---

## 발표 데모 시나리오

### 데모 1 — 이슈부터 PR까지 (코드 레포)

```
/repo https://github.com/Team-STORIX/STORIX-BE-2.0
/issue 42 이 버그 원인 찾아서 고쳐줘
    → gh로 이슈 #42 읽고 → fix/issue-42-xxx 브랜치 생성 → 코드 수정
/ask 방금 수정한 거 꼼꼼한 커밋 메시지로 커밋해줘
/doc 로그인 타임아웃 버그 수정
    → docs/worklog/fix-issue-42.md 에 작업 순서·변경 파일 문서화
/ask 이 브랜치 푸시하고 PR 올려줘
```
**포인트**: Discord 한 채널에서 이슈 → 브랜치 → 수정 → 문서화 → PR이 다 돌아간다.

### 데모 2 — 제2의 뇌에서 블로그로 (볼트 → 발행)

```
/blog AWS RDS 트러블슈팅 정리
    → 옵시디언 볼트에서 RDS 관련 노트를 근거로 읽고
    → _posts/YYYY-MM-DD-....md 를 실제 Jekyll front matter 형식으로 작성
    → 커밋 & main에 push → GitHub Pages 자동 배포
```
**포인트**: 평소 쌓아둔 메모가 Discord 한 줄로 블로그 글이 된다. (Google Open Knowledge Format 흐름과 연결)

### 확장 슬라이드 (개념만)

- 이 구조 그대로 코드 대신 마크다운 볼트를 마운트하면 **개인 LLM Wiki**가 된다.
- 볼트 루트의 `AGENTS.md` = 지식을 에이전트가 잘 읽게 만드는 마크다운 표준(OKF)의 실물 예시.

---

## 아직 검증 안 된 것 (첫 실행 시 확인)

1. **opencode REST 경로** — `bot/bot.py`의 `/session`, `/session/{id}/message` 경로가 실제 API와
   맞는지. 실서버 뜨면 `curl http://localhost:4096/doc` 로 OpenAPI 스펙 확인.
2. **에이전트 bash/git 자동 실행** — opencode 권한 설정에 따라 `git`/`gh` 실행 승인이 막힐 수 있음.
3. **git push 인증** — `entrypoint.sh`의 `gh auth setup-git` 이 동작하는지 (`gh auth status`로 확인).

---

## 디렉토리 구조

```
techtalk/
├── docker-compose.yml
├── .env.example          # .env는 git 제외 (비밀 값)
├── opencode/
│   ├── Dockerfile        # node + opencode + git + gh
│   └── entrypoint.sh     # 시작 시 git push 인증 연결
└── bot/
    ├── Dockerfile
    ├── requirements.txt
    └── bot.py            # 커맨드 정의
```
