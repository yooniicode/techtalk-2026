# techtalk-2026

AWS Cloud Club Tech Talk용 레포지토리


[발표 자료](https://www.figma.com/proto/OijXhRg3edAE8hEFrKmBmf/tech-talk?node-id=3-8388&viewport=371%2C304%2C0.18&t=NrK42GkdzI285Zaq-1&scaling=contain&content-scaling=fixed&starting-point-node-id=3%3A8388&page-id=3%3A629&show-proto-sidebar=1)

Discord에서 슬래시 커맨드로 코딩 에이전트를 부리는 봇이다. 백엔드는 opencode 서버,
모델은 AWS Bedrock의 Claude(Sonnet 4.6 / Haiku 4.5). 코드 레포에서 이슈를 읽고
브랜치를 파서 작업·커밋·PR·리뷰를 하고, 옵시디언 볼트를 참고하거나 블로그 글을 발행한다.

## 아키텍처

```
Discord ──> bot (discord.py) ──HTTP──> opencode serve ──> AWS Bedrock (Claude)
                                          ├─ /workspace  레포 clone (git, gh)
                                          └─ /vault       옵시디언 볼트 (D:/obsidian)
```

- **opencode 컨테이너**: 상시 HTTP 서버. 에이전트가 `git`/`gh`로 clone·브랜치·커밋·push·이슈·PR을 처리한다. 모델은 Bedrock으로 호출.
- **bot 컨테이너**: Discord에 붙어 슬래시 커맨드를 받아 opencode에 프롬프트로 넘긴다.
- opencode 포트는 호스트에 노출하지 않고 compose 내부 네트워크로만 봇이 접근한다. 여기에 basic auth를 더해 이중으로 막는다.
- 에이전트의 파일·bash 실행은 컨테이너 안에 격리된다.

## 사전 준비

### 1. `.env` 만들기

```bash
cp .env.example .env
```

| 변수 | 설명 |
|------|------|
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock Claude 호출용 Bearer 토큰 (리전 us-east-1) |
| `DISCORD_TOKEN` | Discord 봇 토큰 |
| `OPENCODE_PW` | opencode basic auth 비밀번호 (직접 정함, `openssl rand -base64 24`) |
| `GITHUB_TOKEN` | push·이슈·PR용 GitHub PAT |
| `GIT_NAME` / `GIT_EMAIL` | 커밋 작성자 |

### 2. Discord 봇 설정 (Developer Portal)

- **Bot 탭 → Reset Token** → `.env`의 `DISCORD_TOKEN`에 넣기
- **OAuth2 → URL Generator** → scope에 `bot`과 `applications.commands` 둘 다 체크,
  권한은 `Send Messages` + `Read Message History` + `Create Public Threads` → 생성된 URL로 서버에 초대
- 인터랙션 엔드포인트 URL은 비워둔다 (게이트웨이 방식으로 동작).

슬래시 커맨드라서 MESSAGE CONTENT INTENT는 필요 없다.

### 3. Docker Desktop

- Docker Desktop 실행 (Engine running).
- 볼트를 마운트하려면 Settings → Resources → File Sharing에 `D:\`가 포함돼야 한다 (WSL2 백엔드면 기본 포함).

## 띄우기

```bash
docker compose up --build
```

opencode와 bot 두 컨테이너가 뜬다. opencode는 처음 뜰 때 oh-my-opencode-slim을
설치하고 Bedrock 프리셋을 적용하므로 첫 기동에 시간이 좀 걸린다. Discord에서
`/ping`을 쳐서 `opencode: ✅ 연결됨`이 나오면 준비 완료.

종료:

```bash
docker compose down   # 볼륨(작업공간·설정)은 유지된다
```

## 커맨드

채널에서 커맨드를 처음 치면 봇이 주제별 **스레드**를 자동으로 만들고 거기서 작업한다.
같은 스레드 안에서는 opencode 세션을 재사용해 대화 맥락이 이어진다. 오래 걸리는 작업은
스레드 메시지에 지금 무슨 도구를 쓰는지 실시간으로 표시된다.

| 커맨드 | 설명 |
|--------|------|
| `/ping` | 봇·opencode 상태 확인 |
| `/repo <레포>` | GitHub 레포를 작업공간에 clone (내 레포 목록 자동완성) |
| `/branch <이름>` | 브랜치 이동·생성 |
| `/newissue <내용>` | 레포를 살펴보고 잘 정리된 이슈를 생성 (실행 전 승인) |
| `/issue <번호> [지시]` | 이슈를 읽고 연관 브랜치를 만들어 작업 시작 |
| `/ask <요청>` | 레포 작업 + 볼트 참고. 같은 스레드면 맥락 유지 |
| `/diff` | 현재 브랜치 변경사항 요약 (커밋 전 검토) |
| `/pr [제목]` | 현재 브랜치를 push하고 PR 생성 (실행 전 승인) |
| `/review [PR번호]` | PR 또는 현재 브랜치를 코드리뷰 |
| `/doc [제목]` | 브랜치 작업을 git 기록으로 재구성해 문서(.md) 생성 |
| `/note <지시>` | 볼트에 노트 저장·정리 (명시적 쓰기) |
| `/blog <주제>` | 볼트 내용을 근거로 블로그 포스트 작성 → push (실행 전 승인) |
| `/plan <작업>` | Sonnet으로 단계별 계획만 세움 (파일 수정 권한 없음) |
| `/execute [계획]` | Haiku로 계획 실행 (실행 전 승인) |
| `/steer <수정>` | 진행하던 작업의 방향 수정 (세션 이어받음) |
| `/omo <작업>` | oh-my-opencode 오케스트레이터로 멀티에이전트 작업 |
| `/cost` | 이 스레드의 누적 토큰·비용 |

### 모델 라우팅

`/plan`은 Sonnet 4.6(planner 에이전트, 읽기 전용), `/execute`는 Haiku 4.5(executor 에이전트,
쓰기 가능)를 쓴다. 비싼 모델로 설계하고 싼 모델로 실행하는 구성이다. planner는 `edit`/`bash`/`write`
권한이 없어 코드를 건드리지 못한다.

### 승인과 권한

두 층위로 사람이 개입한다.

- **봇 레벨**: `/pr`, `/execute`, `/blog`는 실행 전에 승인/거부 버튼을 띄운다. 요청한 사람만 누를 수 있다.
- **opencode 레벨**: executor가 위험 명령(`rm`, `git push`, `git reset --hard`)을 실행하려 하면
  opencode가 권한을 묻고, 봇이 "한 번만 / 항상 / 거부" 버튼으로 받아 응답한다. 안전한 명령은 그냥 통과한다.

### oh-my-opencode

opencode 위에 얹은 멀티 에이전트 프레임워크. orchestrator가 explorer(탐색)·oracle(설계)·fixer(구현) 등
전문 에이전트에게 작업을 분담한다. 각 역할이 다른 Bedrock 모델을 쓰도록 매핑했다(총괄은 Sonnet, 실무는 Haiku).
`/omo`로 호출한다. 우리가 직접 만든 planner/executor와 같은 서버에 공존한다.

### 에이전트에 항상 주입되는 규칙

- 볼트(`/vault`)는 기본적으로 읽기만. `/note`처럼 명시적으로 요청할 때만 쓴다.
- 커밋은 Conventional Commits 형식. 본문에 무엇을·왜 바꿨는지 적고, 이슈가 있으면 `Refs/Closes #번호`.
  에이전트 서명(Co-authored-by 등)은 넣지 않는다 — 작성자는 사람만.
- 코드 변경 시 관련 문서도 갱신한다.

## 데모

전체 순서는 [REHEARSAL.md](REHEARSAL.md) 참고.

- **코드 작업**: `/newissue`로 이슈를 만들고 `/issue`로 읽어 브랜치 작업 → `/diff` 검토 →
  `/pr`로 PR 생성 → `/review`로 리뷰. 이슈 생성부터 리뷰까지 채널 하나에서 돈다.
- **모델 라우팅**: `/plan`(Sonnet)으로 설계하고 `/execute`(Haiku)로 실행. `/cost`로 비용 대비를 보여준다.
- **멀티에이전트**: `/omo`로 여러 전문 에이전트가 한 작업을 분담.
- **볼트·블로그**: `/note`로 볼트에 쌓고, `/blog`로 볼트 내용을 근거로 글을 써서 발행.

## 디렉토리 구조

```
techtalk/
├── docker-compose.yml
├── .env.example
├── README.md
├── REHEARSAL.md
├── opencode/
│   ├── Dockerfile               # node + opencode + git + gh + bun
│   ├── entrypoint.sh            # 설정 복사, oh-my-opencode 설치, git 인증
│   ├── opencode.json            # 기본 모델, planner/executor 에이전트, 플러그인
│   └── oh-my-opencode-slim.json # oh-my-opencode 역할별 Bedrock 모델 매핑
└── bot/
    ├── Dockerfile
    ├── requirements.txt
    └── bot.py                   # 슬래시 커맨드 정의
```
