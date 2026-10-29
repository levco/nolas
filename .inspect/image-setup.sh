#!/usr/bin/env bash
set -euo pipefail

: "${LEV_DEPENDENCIES_GITHUB_TOKEN:?Set LEV_DEPENDENCIES_GITHUB_TOKEN with read access to levco/lev-common}"

# Keep authentication scoped to the private dependency and out of Git config files.
export GIT_CONFIG_COUNT=4
export GIT_CONFIG_KEY_0=url.https://github.com/levco/lev-common.git.insteadOf
export GIT_CONFIG_VALUE_0=ssh://git@github.com/levco/lev-common.git
export GIT_CONFIG_KEY_1=credential.https://github.com/levco/lev-common.git.useHttpPath
export GIT_CONFIG_VALUE_1=true
export GIT_CONFIG_KEY_2=credential.https://github.com/levco/lev-common.git.helper
export GIT_CONFIG_VALUE_2=
export GIT_CONFIG_KEY_3=credential.https://github.com/levco/lev-common.git.helper
export GIT_CONFIG_VALUE_3='!f() { if [ "$1" = get ]; then printf "username=x-access-token\npassword=%s\n" "$LEV_DEPENDENCIES_GITHUB_TOKEN"; fi; }; f'
export GIT_TERMINAL_PROMPT=0

uv sync --frozen --extra dev
