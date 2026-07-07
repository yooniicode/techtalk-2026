#!/bin/sh
set -e

# GitHub 토큰이 있으면 git이 gh 자격증명을 쓰도록 연결 (git push 인증)
if [ -n "$GH_TOKEN" ]; then
  gh auth setup-git 2>/dev/null || true
fi

# opencode 서버 실행 (CMD로 전달된 인자)
exec "$@"
