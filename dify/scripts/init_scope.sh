#!/usr/bin/env bash
# 创建 PowerContext Scope，并写入一条开场记忆。
# 用法：POWERCONTEXT_URL=http://127.0.0.1:8000 ./scripts/init_scope.sh "Pit Wall 电台"
set -euo pipefail

BASE="${POWERCONTEXT_URL:-http://127.0.0.1:8000}"
TITLE="${1:-Pit Wall 电台}"

scope_id=$(curl -sS -X POST "$BASE/v1/scopes" \
  -H 'content-type: application/json' \
  -d "$(python -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"summary":"车手与工程师的无线电台对话记忆：设定改动、承诺、借口与事故复盘","idempotency_key":sys.argv[2]},ensure_ascii=False))' "$TITLE" "pitwall-scope-$(date +%Y%m%d%H%M%S)")" \
  | python -c 'import json,sys;d=json.load(sys.stdin);print(d.get("scope_id") or d.get("id") or "")')

if [ -z "$scope_id" ]; then
  echo "创建 Scope 失败：请检查 $BASE/v1/scopes 的响应" >&2
  exit 1
fi
echo "scope_id=$scope_id"

curl -sS -X POST "$BASE/v1/sources/content" \
  -H 'content-type: application/json' \
  -d "{\"scope_id\":\"$scope_id\",\"source_id\":\"seed-1\",\"content\":\"车手在银石赛道抱怨高速弯推头，承诺下一场把前翼加两度；工程师提醒他上次加前翼导致直道尾速掉了 2 km/h。\"}"

echo
echo "把上面的 scope_id 填进 Dify 应用的 POWERCONTEXT / SCOPE_ID 环境变量。"
