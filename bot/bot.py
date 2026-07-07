import os
import json
import asyncio
import httpx
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://opencode:4096")
OPENCODE_PW = os.environ.get("OPENCODE_PW", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix는 안 쓰지만 형식상 필요
tree = bot.tree


# 모든 요청 앞에 붙는 공통 규칙 — 작업 환경 + 커밋/문서화 컨벤션
AGENT_RULES = (
    "환경 안내:\n"
    "- 작업 레포는 /workspace/repo 에 있다 (clone된 경우).\n"
    "- 개인 옵시디언 볼트가 /vault 에 마운트되어 있다 (읽기+쓰기 가능). 기본적으로는 "
    "배경·과거 결정·맥락을 참고하는 용도로만 읽어라. 사용자가 명시적으로 저장·기록·정리를 "
    "요청할 때만 볼트에 파일을 쓰거나 수정하라 (요청 없이 임의로 수정 금지).\n"
    "- 블로그 레포는 Jekyll 기반이며 /workspace/blog 에 clone해서 쓴다. 포스트는 "
    "_posts/YYYY-MM-DD-제목.md 형식이고, front matter는 title, date(YYYY-MM-DD HH:MM:SS +0900), "
    "categories, tags:[...], author: yoonji, layout: single, published: true 를 따른다.\n\n"
    "커밋 규칙 (반드시 지킬 것):\n"
    "- 커밋 메시지에 Co-authored-by, 'Generated with', 에이전트/도구 이름 등 어떤 서명도 "
    "붙이지 마라. 작성자는 오직 사람(git 설정의 사용자)만 남긴다.\n"
    "- Conventional Commits 형식을 따른다: `type(scope): 요약` "
    "(type = feat|fix|docs|refactor|test|chore 등).\n"
    "- 요약 줄은 명령형·한 줄. 그 아래 본문에 '무엇을, 왜' 바꿨는지 상세히 적는다 "
    "(어떻게는 코드가 말해주므로 이유 중심).\n"
    "- 관련 이슈가 있으면 본문 끝에 `Refs #<번호>` 또는 `Closes #<번호>`를 넣는다.\n"
    "- 논리적으로 구분되는 변경은 별도 커밋으로 쪼갠다. 한 커밋에 뒤섞지 마라.\n\n"
    "문서화 규칙:\n"
    "- 코드 변경 시 관련 문서(README, docs/ 등)도 함께 갱신한다.\n"
    "- 새 함수·모듈에는 목적을 설명하는 주석/독스트링을 남긴다.\n\n"
    "사용자 요청: "
)


def opencode_headers():
    headers = {"Content-Type": "application/json"}
    if OPENCODE_PW:
        import base64
        token = base64.b64encode(f"opencode:{OPENCODE_PW}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


# 도구 이름 → 사람이 읽을 진행 문구 아이콘
TOOL_LABELS = {
    "read": "📖 파일 읽는 중",
    "write": "✍️ 파일 작성 중",
    "edit": "✏️ 파일 수정 중",
    "bash": "⚙️ 명령 실행 중",
    "grep": "🔍 코드 검색 중",
    "glob": "🔍 파일 탐색 중",
    "list": "📂 디렉토리 확인 중",
    "webfetch": "🌐 웹 조회 중",
    "task": "🧩 하위 작업 실행 중",
}


def describe_tool(part: dict) -> str | None:
    """tool part에서 사람이 읽을 진행 문구를 만든다. 만들 수 없으면 None."""
    if part.get("type") != "tool":
        return None
    tool = part.get("tool", "")
    label = TOOL_LABELS.get(tool, f"🛠️ {tool}")
    inp = part.get("state", {}).get("input", {}) or {}
    target = inp.get("filePath") or inp.get("pattern") or inp.get("command") or ""
    if target:
        target = str(target).replace("/workspace/", "").strip()
        if len(target) > 80:
            target = target[:77] + "..."
        return f"{label}: `{target}`"
    return label


# GitHub 레포 목록 캐시 (full_name 리스트). 봇 시작 시 1회 로드, autocomplete에서 사용.
repo_cache: list[str] = []


async def load_repos() -> list[str]:
    """GitHub API로 접근 가능한 레포 목록(owner/name)을 최신순으로 가져온다."""
    if not GITHUB_TOKEN:
        return []
    repos: list[str] = []
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 최근 업데이트순으로 최대 100개 (데모엔 충분)
            r = await client.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"sort": "updated", "per_page": 100, "affiliation":
                        "owner,collaborator,organization_member"},
            )
            r.raise_for_status()
            repos = [item["full_name"] for item in r.json()]
    except httpx.HTTPError:
        pass
    return repos


async def create_session() -> str:
    """새 opencode 세션을 만들어 ID를 반환한다."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OPENCODE_URL}/session", headers=opencode_headers(), json={},
        )
        r.raise_for_status()
        return r.json()["id"]


# 스레드(채널) ID → 누적 비용/토큰. /cost 커맨드용.
cost_tracker: dict[int, dict] = {}


async def respond_permission(session_id: str, permission_id: str, response: str):
    """권한 요청에 응답한다. response: once | always | reject"""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OPENCODE_URL}/session/{session_id}/permissions/{permission_id}",
            headers=opencode_headers(),
            json={"response": response},
        )
        r.raise_for_status()


async def ask_opencode(prompt: str, agent: str | None = None, on_progress=None,
                       session_id: str | None = None,
                       track_channel: int | None = None,
                       on_permission=None) -> str:
    """opencode에 프롬프트를 보내고 결과를 반환. session_id를 주면 그 세션을
    재사용해 맥락을 이어가고, 없으면 새로 만든다. on_progress(문구)가 주어지면
    작업 중 도구 실행 상황을 실시간으로 콜백한다. track_channel이 주어지면
    그 채널의 비용/토큰을 누적한다. on_permission(정보)이 주어지면 에이전트가
    권한을 요청할 때 호출되어 once/always/reject 중 하나를 반환해야 한다."""
    async with httpx.AsyncClient(timeout=600) as client:
        # 세션 재사용 또는 생성
        if session_id is None:
            r = await client.post(
                f"{OPENCODE_URL}/session", headers=opencode_headers(), json={},
            )
            r.raise_for_status()
            session_id = r.json()["id"]

        # 진행 상황 + 권한 요청 구독 (SSE) — 프롬프트 전송과 병행
        progress_task = None
        if on_progress is not None or on_permission is not None:
            progress_task = asyncio.create_task(
                _stream_progress(session_id, on_progress, on_permission)
            )

        try:
            # 프롬프트 전송 — POST가 작업 완료 후 결과를 동기로 반환한다
            body = {"parts": [{"type": "text", "text": prompt}]}
            if agent:
                body["agent"] = agent  # planner / executor 등 커스텀 에이전트 선택
            r = await client.post(
                f"{OPENCODE_URL}/session/{session_id}/message",
                headers=opencode_headers(),
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        finally:
            if progress_task:
                progress_task.cancel()

        # 비용/토큰 누적 (스레드별) — /cost 커맨드용
        info = data.get("info", {})
        if track_channel is not None:
            acc = cost_tracker.setdefault(
                track_channel, {"cost": 0.0, "input": 0, "output": 0, "calls": 0})
            acc["cost"] += info.get("cost", 0) or 0
            toks = info.get("tokens", {}) or {}
            acc["input"] += toks.get("input", 0) or 0
            acc["output"] += toks.get("output", 0) or 0
            acc["calls"] += 1

        # 에이전트 오류(모델 호출 실패 등) 처리
        err = info.get("error")
        if err:
            detail = err.get("data", {}).get("message", err.get("name", "unknown"))
            return f"⚠️ 에이전트 오류: {detail}"

        # parts[]에서 text 조각만 모아 최종 답변 구성
        parts = data.get("parts", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text.strip() or "(응답 없음)"


async def _stream_progress(session_id: str, on_progress=None, on_permission=None):
    """해당 세션의 tool 실행/권한 요청 이벤트를 구독한다.
    tool 실행 → on_progress(문구), 권한 요청 → on_permission(정보) 호출 후 응답 전송."""
    handled_perms: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", f"{OPENCODE_URL}/event", headers=opencode_headers()
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    props = evt.get("properties", {})
                    if props.get("sessionID") != session_id:
                        continue

                    # 권한 요청 — 사람의 결정을 받아 응답한다
                    if evt.get("type") == "permission.asked" and on_permission:
                        pid = props.get("id")
                        if pid and pid not in handled_perms:
                            handled_perms.add(pid)
                            info = {
                                "permission": props.get("permission", ""),
                                "command": props.get("metadata", {}).get("command", ""),
                                "patterns": props.get("patterns", []),
                            }
                            decision = await on_permission(info)  # once/always/reject
                            await respond_permission(session_id, pid, decision)
                        continue

                    # 도구가 실제로 실행되기 시작할 때만 알림 (중복 최소화)
                    part = props.get("part", {})
                    if on_progress and part.get("type") == "tool" and \
                            part.get("state", {}).get("status") == "running":
                        desc = describe_tool(part)
                        if desc:
                            await on_progress(desc)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass  # 진행 표시는 부가 기능 — 실패해도 본 작업에 영향 없음


async def send_long(interaction: discord.Interaction, header: str, body: str):
    """긴 응답을 Discord 2000자 제한에 맞춰 나눠 보낸다 (첫 조각은 followup edit, 나머지는 추가 전송)."""
    full = f"{header}\n\n{body}" if header else body
    if len(full) <= 1950:
        await interaction.edit_original_response(content=full)
        return
    await interaction.edit_original_response(content=header or "(응답이 길어 분할합니다)")
    for i in range(0, len(body), 1900):
        await interaction.followup.send(body[i:i + 1900])


# 스레드(채널) ID → opencode 세션 ID. 같은 스레드의 요청은 맥락을 이어간다.
thread_sessions: dict[int, str] = {}


async def ensure_thread(interaction: discord.Interaction, title: str) -> discord.abc.Messageable:
    """상호작용이 일어난 곳을 대화 스레드로 확보한다.
    이미 스레드 안이면 그대로 쓰고, 일반 텍스트 채널이면 새 스레드를 만든다."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return channel
    # 텍스트 채널이면 새 스레드 생성 (세션 맥락의 단위)
    if isinstance(channel, discord.TextChannel):
        try:
            thread = await channel.create_thread(
                name=title[:90] or "에이전트 작업",
                type=discord.ChannelType.public_thread,
            )
            return thread
        except discord.HTTPException:
            return channel  # 스레드 생성 실패 시 채널에 그대로
    return channel


async def get_session_for(channel_id: int) -> str:
    """해당 스레드/채널의 세션을 가져오거나 새로 만들어 매핑에 저장한다."""
    sid = thread_sessions.get(channel_id)
    if sid is None:
        sid = await create_session()
        thread_sessions[channel_id] = sid
    return sid


async def edit_long(msg: discord.Message, channel, header: str, body: str):
    """메시지를 최종 답변으로 갱신한다. 길면 첫 조각만 넣고 나머지는 추가 전송."""
    full = f"{header}\n\n{body}" if header else body
    if len(full) <= 1950:
        await msg.edit(content=full)
        return
    await msg.edit(content=header or "(계속)")
    for i in range(0, len(body), 1900):
        await channel.send(body[i:i + 1900])


def thread_title(text: str) -> str:
    """사용자 입력에서 스레드 제목을 만든다 (첫 줄, 최대 60자)."""
    first = (text or "").strip().splitlines()[0] if text.strip() else ""
    first = first.strip()
    if len(first) > 60:
        first = first[:57] + "..."
    return first or "에이전트 작업"


class ConfirmView(discord.ui.View):
    """✅ 승인 / ❌ 거부 버튼. 커맨드를 실행한 사용자만 누를 수 있다."""

    def __init__(self, requester_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.approved: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 작업을 요청한 사람만 결정할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="승인", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.approved = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="거부", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.approved = False
        await interaction.response.defer()
        self.stop()


class PermissionView(discord.ui.View):
    """에이전트 권한 요청에 대한 3버튼: 한번만 / 항상 / 거부. → once/always/reject"""

    def __init__(self, requester_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.decision: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 작업을 요청한 사람만 결정할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="한 번만 허용", style=discord.ButtonStyle.primary, emoji="✅")
    async def once(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.decision = "once"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="항상 허용", style=discord.ButtonStyle.success, emoji="♾️")
    async def always(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.decision = "always"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="거부", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.decision = "reject"
        await interaction.response.defer()
        self.stop()


async def run_agent(interaction: discord.Interaction, prompt: str, header: str,
                    agent: str | None = None, title: str | None = None,
                    confirm: str | None = None):
    """에이전트를 호출하고, 주제별 스레드에서 진행 상황을 실시간 표시한 뒤 결과를 보낸다.
    같은 스레드 안에서는 opencode 세션을 재사용해 맥락을 이어간다.
    title: 새 스레드를 만들 때 쓸 이름 (없으면 header 사용).
    confirm: 값이 있으면 실행 전 ✅/❌ 승인 버튼을 띄우고, 승인해야만 진행한다."""
    channel = interaction.channel
    # 스레드 확보 — 이미 스레드면 그대로, 채널이면 새 스레드 생성
    if isinstance(channel, discord.Thread):
        thread = channel
    else:
        thread = await ensure_thread(interaction, title or header)

    session_id = await get_session_for(thread.id)

    # 원래 커맨드 응답엔 안내, 실제 작업은 스레드의 앵커 메시지에서 진행
    same = getattr(thread, "id", None) == getattr(channel, "id", None)
    await interaction.edit_original_response(
        content="⏳ 시작합니다..." if same else f"🧵 {thread.mention} 에서 진행합니다."
    )

    # 실행 전 승인 (쓰기 작업 등) — 승인해야만 진행
    if confirm:
        view = ConfirmView(interaction.user.id)
        prompt_msg = await thread.send(f"⚠️ **확인 필요**\n{confirm}", view=view)
        await view.wait()
        if view.approved is None:
            await prompt_msg.edit(content="⏱️ 시간 초과 — 작업을 취소했어요.", view=None)
            return
        if not view.approved:
            await prompt_msg.edit(content="❌ 거부됨 — 작업을 취소했어요.", view=None)
            return
        await prompt_msg.edit(content="✅ 승인됨 — 실행합니다.", view=None)

    work_msg = await thread.send("⏳ 작업 준비 중...")

    steps: list[str] = []
    last_edit = 0.0

    async def on_progress(desc: str):
        nonlocal last_edit
        steps.append(desc)
        now = asyncio.get_event_loop().time()
        # Discord rate limit 회피 — 최소 1.5초 간격으로만 편집
        if now - last_edit < 1.5:
            return
        last_edit = now
        body = "⏳ 작업 중...\n" + "\n".join(f"　{s}" for s in steps[-6:])
        try:
            await work_msg.edit(content=body[:1950])
        except discord.HTTPException:
            pass

    async def on_permission(info: dict) -> str:
        """에이전트 권한 요청을 스레드에 버튼으로 띄우고 결정을 받는다."""
        what = info.get("command") or info.get("permission") or "작업"
        view = PermissionView(interaction.user.id)
        pmsg = await thread.send(
            f"🔐 **에이전트가 권한을 요청했어요**\n권한: `{info.get('permission')}`\n"
            f"실행하려는 것: `{str(what)[:300]}`",
            view=view,
        )
        await view.wait()
        decision = view.decision or "reject"  # 시간초과 시 안전하게 거부
        label = {"once": "✅ 한 번만 허용", "always": "♾️ 항상 허용", "reject": "❌ 거부"}[decision]
        await pmsg.edit(content=f"🔐 권한 요청 → {label}", view=None)
        return decision

    try:
        answer = await ask_opencode(prompt, agent=agent, on_progress=on_progress,
                                    session_id=session_id, track_channel=thread.id,
                                    on_permission=on_permission)
    except httpx.HTTPError as e:
        await work_msg.edit(content=f"❌ opencode 연결 오류: {e}")
        return
    await edit_long(work_msg, thread, header, answer)


@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} — in {len(bot.guilds)} guild(s): "
          f"{[g.name for g in bot.guilds]}", flush=True)
    # 봇이 속한 모든 서버에 슬래시 커맨드를 즉시 동기화 (guild 동기화는 반영이 빠름)
    for guild in bot.guilds:
        try:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"  ✅ '{guild.name}'에 {len(synced)}개 커맨드 동기화", flush=True)
        except Exception as e:
            print(f"  ❌ '{guild.name}' 동기화 실패: {e!r} "
                  f"(초대 시 applications.commands scope가 필요할 수 있음)", flush=True)
    # GitHub 레포 목록을 캐싱 (/repo autocomplete용)
    global repo_cache
    repo_cache = await load_repos()
    print(f"  📦 GitHub 레포 {len(repo_cache)}개 캐싱", flush=True)


async def repo_autocomplete(interaction: discord.Interaction, current: str):
    """입력값에 매칭되는 레포를 최대 25개까지 드롭다운으로 제시한다."""
    cur = current.lower()
    matches = [r for r in repo_cache if cur in r.lower()][:25]
    return [discord.app_commands.Choice(name=r, value=r) for r in matches]


@tree.command(name="ask", description="opencode 에이전트에게 질문합니다")
@discord.app_commands.describe(prompt="에이전트에게 시킬 작업/질문")
async def ask(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    await run_agent(interaction, AGENT_RULES + prompt, f"**Q:** {prompt}",
                    title=thread_title(prompt))


@tree.command(name="omo", description="oh-my-opencode 오케스트레이터로 멀티에이전트 작업을 수행합니다")
@discord.app_commands.describe(task="맡길 작업 (탐색·설계·구현을 전문 에이전트가 자동 분담)")
async def omo(interaction: discord.Interaction, task: str):
    await interaction.response.defer(thinking=True)
    await run_agent(interaction, AGENT_RULES + task,
                    "🤖 **멀티에이전트 완료 (oh-my-opencode)**",
                    agent="orchestrator", title=thread_title(f"🤖 {task}"))


@tree.command(name="steer", description="진행 중이던 작업의 방향을 수정합니다 (같은 스레드 세션 이어받음)")
@discord.app_commands.describe(correction="방향 수정 지시 (예: DTO 말고 record로 다시 해줘)")
async def steer(interaction: discord.Interaction, correction: str):
    await interaction.response.defer(thinking=True)
    # 같은 스레드의 세션 맥락을 이어, 직전 작업을 교정하도록 지시
    prompt = AGENT_RULES + (
        "직전까지 진행한 작업의 맥락을 이어간다. 아래는 방향 수정 지시다. "
        "이미 한 작업 중 어긋난 부분을 이 지시에 맞게 되돌리거나 고쳐라:\n\n"
        + correction
    )
    await run_agent(interaction, prompt, f"🧭 방향 수정: {correction}",
                    title=thread_title(f"🧭 {correction}"))


# 채널별 마지막 계획을 저장 — /plan 후 /execute가 이어받는다
last_plan: dict[int, str] = {}


@tree.command(name="plan", description="고가 모델(Sonnet)로 단계별 실행 계획만 세웁니다 (코드 수정 없음)")
@discord.app_commands.describe(task="계획을 세울 작업")
async def plan(interaction: discord.Interaction, task: str):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        "다음 작업에 대한 단계별 실행 계획만 세워라 (코드 수정 금지):\n\n" + task
    )
    try:
        answer = await ask_opencode(prompt, agent="planner")
    except httpx.HTTPError as e:
        await interaction.edit_original_response(content=f"❌ opencode 연결 오류: {e}")
        return
    last_plan[interaction.channel_id] = answer  # /execute가 이어받도록 저장
    await send_long(interaction, "🧠 **계획 (Sonnet 4.6)** — 실행하려면 `/execute`", answer)


@tree.command(name="execute", description="저가 모델(Haiku)로 계획을 실행합니다")
@discord.app_commands.describe(plan_text="실행할 계획 (비우면 이 채널의 마지막 /plan 결과 사용)")
async def execute(interaction: discord.Interaction, plan_text: str = ""):
    await interaction.response.defer(thinking=True)
    plan_body = plan_text.strip() or last_plan.get(interaction.channel_id, "")
    if not plan_body:
        await interaction.edit_original_response(
            content="❌ 실행할 계획이 없어요. 먼저 `/plan`을 실행하거나 plan_text를 넣어주세요.")
        return
    prompt = AGENT_RULES + (
        "다음 계획을 순서대로 정확히 실행하라:\n\n" + plan_body
    )
    await run_agent(interaction, prompt, "⚙️ **실행 완료 (Haiku 4.5)**", agent="executor",
                    title=thread_title(plan_body),
                    confirm="이 계획대로 코드를 수정/실행합니다. 진행할까요?")


@tree.command(name="repo", description="GitHub 레포를 작업공간에 clone합니다")
@discord.app_commands.describe(repo="내 레포 목록에서 선택하거나 URL 직접 입력")
@discord.app_commands.autocomplete(repo=repo_autocomplete)
async def repo(interaction: discord.Interaction, repo: str):
    await interaction.response.defer(thinking=True)
    # owner/name 형식이면 https URL로 변환, 이미 URL이면 그대로
    target = repo.strip()
    if target.startswith("http"):
        url = target
    else:
        url = f"https://github.com/{target}"
    prompt = AGENT_RULES + (
        f"Clone the git repository {url} into /workspace/repo. "
        f"If /workspace/repo already exists, remove it first (rm -rf), then clone fresh. "
        f"After cloning, give me a one-line summary of what this repository is."
    )
    await run_agent(interaction, prompt, f"✅ `{target}` clone",
                    title=f"📦 {target}")


@tree.command(name="branch", description="작업 브랜치로 이동하거나 새로 만듭니다")
@discord.app_commands.describe(name="예: fix/login")
async def branch(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        f"In /workspace/repo, switch to branch '{name}'. "
        f"If it doesn't exist, create it from the current branch (git checkout -b). "
        f"Then report the current branch and its base."
    )
    await run_agent(interaction, prompt, f"🌿 브랜치 `{name}`",
                    title=f"🌿 {name}")


@tree.command(name="issue", description="이슈를 읽고 연관 브랜치를 만들어 작업합니다")
@discord.app_commands.describe(number="이슈 번호", instruction="추가 지시 (선택)")
async def issue(interaction: discord.Interaction, number: int, instruction: str = ""):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        f"In /workspace/repo: read GitHub issue #{number} using `gh issue view {number}`. "
        f"Summarize what the issue asks for, then create a branch named after it "
        f"(e.g. fix/issue-{number}-short-desc) and start working on it. "
    )
    if instruction:
        prompt += f"Additional guidance from the user: {instruction}"
    issue_title = f"🎯 #{number}" + (f" {instruction}" if instruction else "")
    await run_agent(interaction, prompt, f"🎯 이슈 #{number} 작업 시작",
                    title=thread_title(issue_title))


@tree.command(name="doc", description="이번 브랜치 작업을 git 기록으로 문서화합니다")
@discord.app_commands.describe(title="문서 제목 (선택)")
async def doc(interaction: discord.Interaction, title: str = ""):
    await interaction.response.defer(thinking=True)
    slug = title.strip() or "작업 내역"
    prompt = AGENT_RULES + (
        "You are documenting the work done on the current branch of /workspace/repo. "
        "Reconstruct the work ONLY from git — do not invent steps. Investigate with:\n"
        "- `git branch --show-current` and the base branch it diverged from "
        "(`git merge-base` against the default branch)\n"
        "- `git log <base>..HEAD --stat` for the commit sequence (order, messages, files)\n"
        "- `git diff <base>...HEAD` for the actual changes, and `git status` for uncommitted work\n"
        "- If a related issue is referenced, `gh issue view <n>` for the goal\n\n"
        "Then write a detailed Korean markdown document to "
        "`/workspace/repo/docs/worklog/<branch-name>.md` with these sections:\n"
        f"# {slug}\n"
        "## 개요 — 이 브랜치가 해결하려는 문제 / 목표 (관련 이슈 있으면 링크)\n"
        "## 작업 순서 — 커밋을 시간순으로, 각 커밋이 '무엇을 왜' 바꿨는지 번호 매겨 설명\n"
        "## 주요 변경 파일 — 파일별로 무엇이 어떻게 바뀌었는지\n"
        "## 미완료 / 다음 할 일 — 아직 커밋 안 된 변경이나 남은 작업\n\n"
        "Create the docs/worklog directory if needed. "
        "After writing the file, reply with the file path and a short summary of the document."
    )
    await run_agent(interaction, prompt, "✅ 문서화 완료",
                    title=f"📝 {slug}")


@tree.command(name="note", description="볼트에 내용을 저장/정리합니다 (명시적 쓰기)")
@discord.app_commands.describe(instruction="예: 오늘 배운 Docker 볼륨 개념 정리해서 저장")
async def note(interaction: discord.Interaction, instruction: str):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        "The user is explicitly asking you to WRITE to the Obsidian vault at /vault. "
        "Create or update the appropriate markdown note. Follow the vault's existing "
        "folder structure and note conventions (check nearby notes first). "
        "Use [[wiki-links]] to connect related notes where sensible. "
        f"After saving, report the file path.\n\nRequest: {instruction}"
    )
    await run_agent(interaction, prompt, "🗒️ 볼트 기록",
                    title=thread_title(f"🗒️ {instruction}"))


@tree.command(name="blog", description="볼트 내용을 바탕으로 블로그 포스트를 작성해 발행합니다")
@discord.app_commands.describe(topic="예: AWS RDS 트러블슈팅 정리")
async def blog(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        "Write and publish a blog post to the Jekyll blog repository.\n"
        "Steps:\n"
        "1. If /workspace/blog does not exist, clone "
        "https://github.com/yooniicode/yooniicode.github.io into /workspace/blog. "
        "If it exists, `git pull` to update, and make sure you are on the main branch.\n"
        "2. Research the topic by reading relevant notes in the Obsidian vault at /vault. "
        "Base the post's content on what you find there — do not invent facts.\n"
        "3. Write a new post at /workspace/blog/_posts/YYYY-MM-DD-<slug>.md using today's date. "
        "Follow the exact front matter convention of existing posts "
        "(title, date with +0900, categories, tags, author: yoonji, layout: single, published: true). "
        "Write the body in Korean, well-structured with headings.\n"
        "4. Commit the new post (conventional commit, no co-author signature) and push to main.\n"
        "5. Report the file path, the post title, and confirm the push succeeded.\n\n"
        f"Blog topic: {topic}"
    )
    await run_agent(interaction, prompt, "✍️ 블로그 발행 완료",
                    title=thread_title(f"✍️ {topic}"),
                    confirm="블로그 포스트를 작성하고 main에 push합니다. 진행할까요?")


@tree.command(name="diff", description="현재 브랜치의 변경사항을 요약해 보여줍니다 (커밋 전 검토)")
async def diff(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        "In /workspace/repo, show what has changed on the current branch so a human can "
        "review before committing. Run `git status` and `git diff` (staged + unstaged), and "
        "if there are commits ahead of the base branch, include `git diff <base>...HEAD`. "
        "Summarize in Korean: 어떤 파일이 어떻게 바뀌었는지 파일별로, 그리고 주의해서 볼 지점. "
        "Do NOT commit or modify anything — read-only review."
    )
    await run_agent(interaction, prompt, "🔍 변경사항 검토", title="🔍 diff 검토")


@tree.command(name="pr", description="현재 브랜치를 push하고 Pull Request를 생성합니다")
@discord.app_commands.describe(title="PR 제목 (선택, 없으면 커밋 기반 자동)")
async def pr(interaction: discord.Interaction, title: str = ""):
    await interaction.response.defer(thinking=True)
    prompt = AGENT_RULES + (
        "In /workspace/repo, create a Pull Request for the current branch.\n"
        "Steps:\n"
        "1. Ensure all work is committed (if there are uncommitted changes, commit them with "
        "a proper conventional commit message).\n"
        "2. Push the current branch to origin (`git push -u origin <branch>`).\n"
        "3. Create a PR with `gh pr create`. Write a clear Korean title and body: "
        "요약, 변경 내용, 관련 이슈(있으면 Closes #n). "
        + (f"Use this as the PR title: {title}\n" if title else "")
        + "4. Report the PR URL so the user can click it."
    )
    await run_agent(interaction, prompt, "🔀 PR 생성 완료", title="🔀 Pull Request",
                    confirm="현재 브랜치를 push하고 PR을 생성합니다. 진행할까요?")


@tree.command(name="review", description="PR 또는 현재 브랜치를 코드리뷰합니다")
@discord.app_commands.describe(number="리뷰할 PR 번호 (비우면 현재 브랜치 변경분)")
async def review(interaction: discord.Interaction, number: int = 0):
    await interaction.response.defer(thinking=True)
    if number:
        target = (
            f"Review pull request #{number}. Fetch it with `gh pr diff {number}` and "
            f"`gh pr view {number}` for context."
        )
        head = f"🧐 PR #{number} 코드리뷰"
        ttl = f"🧐 PR #{number} 리뷰"
    else:
        target = (
            "Review the changes on the current branch versus its base branch "
            "(`git diff <base>...HEAD`)."
        )
        head = "🧐 코드리뷰"
        ttl = "🧐 코드리뷰"
    prompt = AGENT_RULES + (
        "You are a senior code reviewer. " + target + "\n"
        "한국어로 리뷰하라. 다음을 짚어라: 버그·엣지케이스, 보안 위험, 설계·가독성 개선점, "
        "테스트 누락. 각 지적은 파일:라인과 함께 근거를 들고, 중요도(높음/중간/낮음)를 표시하라. "
        "칭찬할 점도 짧게. 코드를 수정하지 말고 리뷰 코멘트만 작성하라 (read-only)."
    )
    await run_agent(interaction, prompt, head, title=ttl)


@tree.command(name="cost", description="이 스레드에서 지금까지 쓴 토큰/비용을 보여줍니다")
async def cost(interaction: discord.Interaction):
    acc = cost_tracker.get(interaction.channel_id)
    if not acc or acc["calls"] == 0:
        await interaction.response.send_message(
            "아직 이 스레드에서 집계된 사용량이 없어요. (에이전트 작업 후 다시 확인)",
            ephemeral=True)
        return
    msg = (
        f"💰 **이 스레드 누적 사용량**\n"
        f"　• 호출 횟수: {acc['calls']}회\n"
        f"　• 입력 토큰: {acc['input']:,}\n"
        f"　• 출력 토큰: {acc['output']:,}\n"
        f"　• 누적 비용: **${acc['cost']:.4f}**"
    )
    await interaction.response.send_message(msg)


@tree.command(name="ping", description="봇 및 opencode 서버 상태 확인")
async def ping(interaction: discord.Interaction):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OPENCODE_URL}/app", headers=opencode_headers())
            status = "✅ 연결됨" if r.status_code == 200 else f"⚠️ HTTP {r.status_code}"
    except httpx.HTTPError:
        status = "❌ 연결 실패"
    await interaction.response.send_message(f"Bot: ✅ | opencode: {status}")


bot.run(DISCORD_TOKEN)
