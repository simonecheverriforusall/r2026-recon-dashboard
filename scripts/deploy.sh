#!/usr/bin/env bash
# Deploy r2026-recon-dashboard to GitHub + Render.
# Requires: gh (logged in), render (logged in or RENDER_API_KEY set)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REPO_NAME="${REPO_NAME:-r2026-recon-dashboard}"
GITHUB_OWNER="${GITHUB_OWNER:-}"
RENDER_SERVICE_NAME="${RENDER_SERVICE_NAME:-r2026-recon-dashboard}"
RENDER_REGION="${RENDER_REGION:-oregon}"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v gh >/dev/null || die "Install GitHub CLI: brew install gh"
command -v render >/dev/null || die "Install Render CLI: brew install render"

if ! render workspace set "${RENDER_WORKSPACE:-simon-forusall}" -o text --confirm >/dev/null 2>&1; then
  echo "Setting Render workspace…"
  render workspaces -o json --confirm | python3 -c "import json,sys; ws=json.load(sys.stdin); print('Available:', [w['name'] for w in ws])"
  die "Run: render workspace set <name>"
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI not authenticated. Opening browser login…"
  gh auth login -h github.com -p https -w
fi

if [[ -z "${RENDER_API_KEY:-}" ]] && ! render whoami -o json --confirm >/dev/null 2>&1; then
  echo "Render CLI not authenticated. Opening browser login…"
  render login
fi

echo ""
echo "Before creating the service, ensure:"
echo "  1. Render GitHub app can access this repo:"
echo "     https://github.com/apps/render/installations/new"
echo "  2. A payment method is on file (required even for free tier):"
echo "     https://dashboard.render.com/billing"
echo ""
read -r -p "Press Enter when both are done, or Ctrl+C to abort…"

[[ -f .env ]] || die "Missing .env — copy .env.example and fill in Jira credentials."

if [[ ! -d .git ]]; then
  git init
  git branch -M main
fi

if ! git config user.email >/dev/null; then
  GIT_EMAIL="$(gh api user -q .email 2>/dev/null || true)"
  GIT_NAME="$(gh api user -q .name 2>/dev/null || true)"
  [[ -n "$GIT_NAME" ]] && git config user.name "$GIT_NAME"
  [[ -n "$GIT_EMAIL" ]] && git config user.email "$GIT_EMAIL"
fi

git add -A
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Initial r2026 reconciliation dashboard"
fi

if [[ -z "$GITHUB_OWNER" ]]; then
  GITHUB_OWNER="$(gh api user -q .login)"
fi

if ! gh repo view "${GITHUB_OWNER}/${REPO_NAME}" >/dev/null 2>&1; then
  echo "Creating GitHub repo ${GITHUB_OWNER}/${REPO_NAME}…"
  gh repo create "$REPO_NAME" --private --source=. --remote=origin --push
else
  git remote add origin "https://github.com/${GITHUB_OWNER}/${REPO_NAME}.git" 2>/dev/null || true
  git push -u origin main
fi

REPO_URL="https://github.com/${GITHUB_OWNER}/${REPO_NAME}"

set -a
# shellcheck disable=SC1091
source .env
set +a

[[ -n "${JIRA_BASE_URL:-}" ]] || die "JIRA_BASE_URL missing in .env"
[[ -n "${JIRA_EMAIL:-}" ]] || die "JIRA_EMAIL missing in .env"
[[ -n "${JIRA_API_TOKEN:-}" ]] || die "JIRA_API_TOKEN missing in .env"

RENDER_ARGS=(
  services create
  --name "$RENDER_SERVICE_NAME"
  --type web_service
  --repo "$REPO_URL"
  --branch main
  --runtime docker
  --plan free
  --region "$RENDER_REGION"
  --health-check-path /api/health
  --env-var "JIRA_BASE_URL=${JIRA_BASE_URL}"
  --env-var "JIRA_EMAIL=${JIRA_EMAIL}"
  --env-var "JIRA_API_TOKEN=${JIRA_API_TOKEN}"
  --env-var "JIRA_PROJECT_KEY=${JIRA_PROJECT_KEY:-R2026}"
  --auto-deploy
  --output json
  --confirm
)

if render services list -o json --confirm 2>/dev/null | grep -q "\"name\":\"${RENDER_SERVICE_NAME}\""; then
  echo "Render service '${RENDER_SERVICE_NAME}' already exists."
  SVC_ID="$(render services list -o json --confirm | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s.get('name')=='${RENDER_SERVICE_NAME}':
        print(s.get('id','')); break
")"
  [[ -n "$SVC_ID" ]] && render deploys create "$SVC_ID" --wait --confirm -o json
else
  echo "Creating Render service…"
  render "${RENDER_ARGS[@]}"
fi

echo ""
echo "Done."
echo "  GitHub:  ${REPO_URL}"
echo "  Render:  https://dashboard.render.com"
echo "  Next:    Add Cloudflare Access — see DEPLOY.md Part 2"
