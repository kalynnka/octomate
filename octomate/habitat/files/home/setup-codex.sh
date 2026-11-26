#!/bin/sh
set -eu
mode="${SBX_CRED_OPENAI_MODE:-none}"
case "$mode" in
  oauth|apikey|none) ;;
  *) echo "Unsupported sbx OpenAI credential mode" >&2; exit 1 ;;
esac
umask 077
mkdir -p "$OCTOMATE_HABITAT_CONFIG/.codex"
config="$OCTOMATE_HABITAT_CONFIG/.codex/config.toml"
auth="$OCTOMATE_HABITAT_CONFIG/.codex/auth.json"
: > "$config"
if [ "$mode" = none ]; then
  rm -f "$auth"
  exit 0
fi
printf 'forced_login_method = "api"\n' > "$config"
if [ "$mode" = oauth ]; then
  cat >> "$config" << 'EOF'
model_provider = "sandboxd"
[model_providers.sandboxd]
name = "Sandbox Proxy"
base_url = "https://chatgpt.com/backend-api/codex"
experimental_bearer_token = "oai-oat01-proxy-managed"
requires_openai_auth = false
EOF
fi
printf '{"OPENAI_API_KEY":"proxy-managed"}\n' > "$auth"
chmod 600 "$auth"
