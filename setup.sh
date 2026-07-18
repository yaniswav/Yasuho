#!/usr/bin/env bash
#
# Yasuho one-command setup (no Docker). Idempotent - safe to re-run.
#
# Provisions, on a fresh Linux host:
#   1. a Python virtualenv (.venv) + all dependencies
#   2. config/*.ini from the templates (if missing)
#   3. a Fernet encryption key (auto-generated, for AniList token encryption)
#   4. the local PostgreSQL role + database, and wires the DSN into config/bot.ini
#
# The database schema itself is created automatically by the bot on first start.
#
# Usage:  ./setup.sh
# Override the interpreter:  PYTHON=python3.10 ./setup.sh
#
# Secrets live in files this script touches: keep everything owner-only.
umask 077

set -uo pipefail
cd "$(dirname "$0")"

info()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
errx()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

DB_NAME="yasuho_db"
DB_USER="yasuho"

# ---- 1. Python + virtualenv + dependencies -------------------------------
PYTHON="${PYTHON:-$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)}"
[ -n "$PYTHON" ] || errx "No suitable Python found. Install python3.12+ (the music backend sonolink needs 3.12+)."
info "Using interpreter: $PYTHON ($("$PYTHON" --version 2>&1))"
# discord.py needs Python 3.8+; the music backend (sonolink) needs 3.12+.
"$PYTHON" -c 'import sys; sys.exit(0 if (3, 8) <= sys.version_info[:2] < (3, 14) else 1)' \
    || errx "Python 3.8-3.13 required (found $("$PYTHON" --version 2>&1)). Try: PYTHON=python3.13 ./setup.sh"

if [ ! -d .venv ]; then
    info "Creating virtualenv (.venv)..."
    "$PYTHON" -m venv .venv || errx "Failed to create the virtualenv (try: sudo apt install ${PYTHON##*/}-venv)."
fi
VENV_PY="./.venv/bin/python"

info "Installing dependencies (this can take a minute)..."
"$VENV_PY" -m pip install -q -U pip || warn "pip self-upgrade failed, continuing."
"$VENV_PY" -m pip install -q -r requirements.txt || errx "Dependency install failed."

# ---- 2. config/*.ini: restore from backup, else scaffold from templates --
CONFIG_BACKUP="${YASUHO_CONFIG_BACKUP:-$HOME/.yasuho-config-backup}"
mkdir -p config
# Prefer restoring the real secrets from the out-of-repo backup (this survives a
# `git clean -fdx`); only fall back to the templates when there is no backup.
for f in bot.ini tokens.ini; do
    if [ ! -f "config/$f" ] && [ -f "$CONFIG_BACKUP/$f" ]; then
        cp "$CONFIG_BACKUP/$f" "config/$f"
        info "Restored config/$f from the backup at $CONFIG_BACKUP."
    fi
done
for tpl in config/*.template.ini; do
    [ -e "$tpl" ] || continue
    real="${tpl%.template.ini}.ini"
    if [ ! -f "$real" ]; then
        cp "$tpl" "$real"
        info "Created $real (fill in your real values)."
    fi
done

# ---- 3. Fernet encryption key (auto) -------------------------------------
if [ -f config/tokens.ini ] && grep -qE '^[[:space:]]*fernetKey[[:space:]]*=[[:space:]]*(YOUR_GENERATED_FERNET_KEY)?[[:space:]]*$' config/tokens.ini; then
    KEY="$("$VENV_PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    sed -i "s|^[[:space:]]*fernetKey[[:space:]]*=.*|fernetKey = ${KEY}|" config/tokens.ini
    info "Generated and stored a Fernet encryption key."
fi

# ---- 4. PostgreSQL role + database ---------------------------------------
# Skip the whole step (and the sudo it needs) when the DB is already reachable
# via the configured DSN - so re-running setup.sh never re-prompts for sudo.
DSN="$(grep -E '^[[:space:]]*PostgreSQL[[:space:]]*=' config/bot.ini 2>/dev/null | head -1 | sed 's/^[^=]*=[[:space:]]*//')"
if [ -n "$DSN" ] && command -v psql >/dev/null 2>&1 && psql "$DSN" -c 'SELECT 1' >/dev/null 2>&1; then
    info "Database already reachable via the configured DSN - skipping PostgreSQL setup."
elif command -v psql >/dev/null 2>&1; then
    pg() { sudo -u postgres psql -tAc "$1" 2>/dev/null; }
    if [ "$(pg "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")" != "1" ]; then
        DB_PASS="$("$VENV_PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
        info "Creating PostgreSQL role '${DB_USER}'..."
        if sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';" >/dev/null; then
            sed -i "s|^[[:space:]]*PostgreSQL[[:space:]]*=.*|PostgreSQL = postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}|" config/bot.ini
            info "Wrote the database DSN into config/bot.ini."
        else
            warn "Could not create the role (need sudo/postgres access?). Set the DSN in config/bot.ini manually."
        fi
    else
        warn "Role '${DB_USER}' exists but the configured DSN cannot authenticate (lost password after a config wipe?)."
        if [ -t 0 ]; then
            read -rp "Reset the '${DB_USER}' password now and rewrite the DSN? (your data is kept) [y/N] " ans
            if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
                DB_PASS="$("$VENV_PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
                if sudo -u postgres psql -c "ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASS}';" >/dev/null; then
                    sed -i "s|^[[:space:]]*PostgreSQL[[:space:]]*=.*|PostgreSQL = postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}|" config/bot.ini
                    info "Reset the password and rewrote the DSN. The database and its data are untouched."
                else
                    warn "Could not reset the password (need sudo/postgres access). Do it manually with ALTER ROLE."
                fi
            fi
        else
            warn "Re-run setup.sh in a terminal to reset the password, or set the DSN in config/bot.ini manually."
        fi
    fi
    if [ "$(pg "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")" != "1" ]; then
        info "Creating database '${DB_NAME}'..."
        sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" >/dev/null \
            || warn "Could not create the database - create it manually."
    else
        info "Database '${DB_NAME}' already exists."
    fi
else
    warn "PostgreSQL ('psql') not found. Install it (e.g. 'sudo apt install postgresql') and re-run this script."
fi

# ---- 5. Remaining manual secret (the bot token) --------------------------
if [ -f config/bot.ini ] && grep -qE '^[[:space:]]*Token[[:space:]]*=[[:space:]]*"?YOUR_BOT_TOKEN"?[[:space:]]*$' config/bot.ini; then
    if [ -t 0 ]; then
        read -rp "Paste your Discord bot token (or leave blank to do it later): " TOKEN
        if [ -n "$TOKEN" ]; then
            sed -i "s|^[[:space:]]*Token[[:space:]]*=.*|Token = ${TOKEN}|" config/bot.ini
            info "Stored your bot token."
        fi
    else
        warn "Set your Discord bot token in config/bot.ini -> [Bot_Token] Token."
    fi
fi

# ---- 6. Back up the filled-in secrets outside the repo -------------------
if [ -f config/bot.ini ] && ! grep -q 'YOUR_BOT_TOKEN' config/bot.ini; then
    mkdir -p "$CONFIG_BACKUP" && chmod 700 "$CONFIG_BACKUP" 2>/dev/null
    cp -f config/bot.ini "$CONFIG_BACKUP/bot.ini"
    [ -f config/tokens.ini ] && cp -f config/tokens.ini "$CONFIG_BACKUP/tokens.ini"
    info "Backed up config to $CONFIG_BACKUP - it survives 'git clean -fdx' and rm."
fi

echo
info "Setup complete. Optional features (AniList, lyrics, weather, top.gg...) need their keys in config/tokens.ini."
info "Start the bot with:  ./run.sh        (auto-restart loop)"
info "The database schema is created automatically on first start."
