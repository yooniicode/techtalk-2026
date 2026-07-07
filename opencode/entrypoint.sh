#!/bin/sh
set -e

CONFIG_DIR=/root/.config/opencode
CONFIG=$CONFIG_DIR/opencode.json
mkdir -p "$CONFIG_DIR"

# config 볼륨이 비어 있으면 이미지의 기본 설정을 복사 (최초 1회)
if [ ! -f "$CONFIG" ]; then
  cp /opt/opencode-default.json "$CONFIG"
fi

# oh-my-opencode-slim 설치/갱신 (멱등). skills와 에이전트 설정을 config 볼륨에 심는다.
# 이미 plugin 항목이 있으면 opencode가 자동 로드하므로 실패해도 서버는 정상 동작.
if [ -n "$OMO_INSTALL" ]; then
  bunx oh-my-opencode-slim@latest install --no-tui --skills=yes \
    --background-subagents=no --reset 2>&1 | tail -20 || \
    echo "[entrypoint] oh-my-opencode 설치 경고 — 계속 진행"
  # 설치가 만든 OpenAI 프리셋을 우리 Bedrock 프리셋으로 교체
  cp /opt/oh-my-opencode-slim.json "$CONFIG_DIR/oh-my-opencode-slim.json"
  echo "[entrypoint] oh-my-opencode를 Bedrock 프리셋으로 설정"
fi

# GitHub 토큰이 있으면 git이 gh 자격증명을 쓰도록 연결 (git push 인증)
if [ -n "$GH_TOKEN" ]; then
  gh auth setup-git 2>/dev/null || true
fi

# opencode 서버 실행 (CMD로 전달된 인자)
exec "$@"
