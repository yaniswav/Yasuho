#!/usr/bin/env bash
#
# Run Yasuho with an auto-restart loop. Uses the virtualenv created by ./setup.sh,
# falling back to the system python if no venv is present.
#
# Auto-update: before each start the script can pull the latest version from the
# git remote. It ONLY ever fast-forwards a clean, non-diverged checkout, so it can
# never clobber local changes or your gitignored config (bot.ini / tokens.ini).
# Toggle it with the AUTO_UPDATE variable below, or per-run: AUTO_UPDATE=0 ./run.sh
#
# Secrets live in files this script touches: keep everything owner-only.
umask 077

cd "$(dirname "$0")"

AUTO_UPDATE="${AUTO_UPDATE:-1}"

if [ -x ./.venv/bin/python ]; then
    PY=./.venv/bin/python
else
    PY="$(command -v python3.11 || command -v python3.10 || command -v python3)"
    echo "[run] No .venv found - using $PY (run ./setup.sh to create one)."
fi

# --- Out-of-repo backup of the gitignored secret config -----------------------
# `git clean -fdx` and stray rm's can wipe config/bot.ini + config/tokens.ini
# (they are gitignored, so NOT recoverable from git). Keep a copy OUTSIDE the
# repo and restore it automatically if the working copy goes missing.
CONFIG_BACKUP="${YASUHO_CONFIG_BACKUP:-$HOME/.yasuho-config-backup}"

restore_config() {
    for f in bot.ini tokens.ini; do
        if [ ! -f "config/$f" ] && [ -f "$CONFIG_BACKUP/$f" ]; then
            mkdir -p config
            cp -f "$CONFIG_BACKUP/$f" "config/$f"
            echo "[run] Restored config/$f from $CONFIG_BACKUP."
        fi
    done
}

backup_config() {
    # Only snapshot a filled-in config (never a placeholder template) so a
    # broken config can never overwrite a good backup.
    [ -f config/bot.ini ] || return 0
    grep -q 'YOUR_BOT_TOKEN' config/bot.ini && return 0
    mkdir -p "$CONFIG_BACKUP" && chmod 700 "$CONFIG_BACKUP" 2>/dev/null
    cp -f config/bot.ini "$CONFIG_BACKUP/bot.ini" 2>/dev/null
    [ -f config/tokens.ini ] && cp -f config/tokens.ini "$CONFIG_BACKUP/tokens.ini" 2>/dev/null
}

self_update() {
    [ "$AUTO_UPDATE" = "1" ] || return 0
    command -v git >/dev/null 2>&1 || return 0
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0

    echo "[run] Checking for updates..."
    if ! git fetch --quiet origin 2>/dev/null; then
        echo "[run] Could not reach the remote - starting with the local version."
        return 0
    fi

    if ! git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
        echo "[run] No upstream branch - skipping update."
        return 0
    fi

    local local_rev remote_rev base
    local_rev="$(git rev-parse @)"
    remote_rev="$(git rev-parse '@{u}')"
    base="$(git merge-base @ '@{u}')"

    if [ "$local_rev" = "$remote_rev" ]; then
        echo "[run] Already up to date."
        return 0
    fi
    if [ "$local_rev" != "$base" ]; then
        echo "[run] Local branch has diverged from the remote - skipping auto-update."
        return 0
    fi
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "[run] Local changes present - skipping auto-update to avoid conflicts."
        return 0
    fi

    echo "[run] Update available - fast-forwarding..."
    local req_before req_after
    req_before="$(git rev-parse 'HEAD:requirements.txt' 2>/dev/null)"
    if git merge --ff-only --quiet '@{u}'; then
        req_after="$(git rev-parse 'HEAD:requirements.txt' 2>/dev/null)"
        if [ "$req_before" != "$req_after" ]; then
            echo "[run] requirements.txt changed - installing dependencies..."
            "$PY" -m pip install -q -r requirements.txt || echo "[run] Dependency install failed - continuing."
        fi
        echo "[run] Updated to $(git rev-parse --short HEAD)."
    else
        echo "[run] Fast-forward failed - starting with the local version."
    fi
}

# Recover the secrets if a clean/rm wiped them, then snapshot a good config.
restore_config
backup_config

while true; do
    self_update
    echo "Starting the bot..."
    "$PY" core.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "The bot has stopped normally. Exiting."
        break
    fi
    echo "The bot stopped with an error (code $EXIT_CODE). Restarting in 5 seconds..."
    sleep 5
done
