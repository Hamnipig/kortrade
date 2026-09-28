#!/usr/bin/env bash
# 데이터 커밋 + push — **바이너리 DB 충돌을 SQLite 레벨에서 푼다.**
#
# 왜 이 스크립트가 있는가 (2026-09-28 실측 사고)
#   update.yml 과 flash.yml 이 같은 data/kortrade.sqlite 를 같은 브랜치에 커밋한다.
#   커밋 직전에 상대가 먼저 push 했으면 `git pull --rebase` 가 이렇게 죽는다:
#
#       warning: Cannot merge binary files: data/kortrade.sqlite
#       CONFLICT (content): Merge conflict in data/kortrade.sqlite
#       fatal: You are not currently on a branch.      ← rebase 중단, detached HEAD
#       Error: Process completed with exit code 128
#
#   sqlite 는 텍스트 merge 가 **원리적으로** 불가능하므로 rebase 로는 절대 못 푼다.
#   그렇다고 한쪽을 버리면(--ours/--theirs) 버린 쪽이 방금 쓴 수백~수천 API 콜이다.
#
#   → git 대신 **scripts/merge_db.py 로 행 단위 병합**한다. 모든 표가 자연키 UNIQUE +
#     UPSERT 구조라 정확히 합쳐진다. 충돌 행은 이번 실행 값(최신)이 이기고,
#     상대에만 있던 행은 추가된다. 그 뒤 site/data 를 DB 에서 다시 만든다.
#
# 사용:  bash scripts/commit_push.sh "data: 2026-09-28 관세청 수출입 갱신"
set -uo pipefail
MSG="${1:?커밋 메시지가 필요합니다}"
BRANCH="${2:-main}"
TRIES="${3:-5}"

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

stage() { git add -A data site/data 2>/dev/null || git add -A data || true; }

stage
if git diff --staged --quiet; then
  echo "변경 없음 — 커밋 건너뜀"
  exit 0
fi
git commit -m "$MSG" || true

for i in $(seq 1 "$TRIES"); do
  git fetch origin "$BRANCH" || true

  # 원격이 우리 조상이면(=우리가 앞서 있으면) 그냥 밀어넣는다
  if git merge-base --is-ancestor "origin/$BRANCH" HEAD 2>/dev/null; then
    if git push origin "HEAD:$BRANCH"; then
      echo "push 완료 (시도 $i)"
      exit 0
    fi
    echo "push 거절됨 — 원격이 그 사이 또 움직였습니다 (시도 $i)"
  else
    echo "원격이 앞서 있습니다 — DB 를 행 단위로 병합합니다 (시도 $i)"
  fi

  # 이번 실행의 산출물을 따로 빼 둔다
  rm -rf /tmp/kt-mine && mkdir -p /tmp/kt-mine
  cp -a data /tmp/kt-mine/ 2>/dev/null || true
  cp -a site/data /tmp/kt-mine/site-data 2>/dev/null || true

  # 원격 DB 를 꺼내 두고 원격 상태로 리셋
  git show "origin/$BRANCH:data/kortrade.sqlite" > /tmp/kt-remote.sqlite 2>/dev/null \
    || rm -f /tmp/kt-remote.sqlite
  git reset --hard "origin/$BRANCH" || true

  # 우리 산출물을 되돌리고, 원격 DB 의 행을 우리 DB 에 붓는다
  cp -a /tmp/kt-mine/data/. data/ 2>/dev/null || true
  mkdir -p site/data && cp -a /tmp/kt-mine/site-data/. site/data/ 2>/dev/null || true
  if [ -f /tmp/kt-remote.sqlite ]; then
    python scripts/merge_db.py --into data/kortrade.sqlite --from /tmp/kt-remote.sqlite || true
  fi

  # site/data 는 DB 에서 나오는 파생물이다. 병합했으면 다시 만들어야 맞다.
  # 어느 하나가 실패해도 나머지는 계속 간다 (수집이 부분 실패했을 수 있다).
  for b in build_site build_watchlist build_universe build_chains build_battery build_flash; do
    python "scripts/$b.py" >/dev/null 2>&1 || echo "  (재생성 건너뜀: $b)"
  done

  stage
  git diff --staged --quiet || git commit -m "$MSG"
done

echo "::error::${TRIES}회 시도 후에도 push 하지 못했습니다. 원격이 계속 앞서고 있습니다 — 워크플로가 동시에 도는지 확인하세요."
exit 1
