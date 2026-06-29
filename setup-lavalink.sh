#!/usr/bin/env bash
#
# Download, configure and run a Lavalink v4 server for Yasuho's music feature.
# Lavalink is the audio backend SonoLink connects to; run this on the bot host.
#
# Usage:
#   ./setup-lavalink.sh            download + write config, then print how to run
#   ./setup-lavalink.sh start      ... and launch it now in the foreground
#   ./setup-lavalink.sh systemd    ... and install a systemd service (needs sudo)
#   ./setup-lavalink.sh update     re-download the latest Lavalink.jar
#
# Override defaults via env, e.g.:
#   LAVALINK_PASSWORD='strongpass' LAVALINK_PORT=2333 ./setup-lavalink.sh
#
cd "$(dirname "$0")"

LAVALINK_DIR="${LAVALINK_DIR:-$(pwd)/lavalink}"
LAVALINK_PORT="${LAVALINK_PORT:-2333}"
LAVALINK_PASSWORD="${LAVALINK_PASSWORD:-youshallnotpass}"
YOUTUBE_PLUGIN_VERSION="${YOUTUBE_PLUGIN_VERSION:-1.13.5}"
HEAP="${HEAP:-512m}"
JAR_URL="https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar"

info() { printf '[lavalink] %s\n' "$1"; }
errx() { printf '[lavalink][error] %s\n' "$1" >&2; exit 1; }

# 1. Java 17+ is required by Lavalink v4.
command -v java >/dev/null 2>&1 || errx "Java not found. Install Java 17+ (e.g. Temurin)."
java_major="$(java -version 2>&1 | head -1 | grep -oE '[0-9]+' | head -1)"
if ! [ "${java_major:-0}" -ge 17 ] 2>/dev/null; then
    errx "Java 17+ required (found: $(java -version 2>&1 | head -1))."
fi
info "Java OK ($(java -version 2>&1 | head -1))."

# 2. Download the latest Lavalink.jar (kept unless 'update' is requested).
mkdir -p "$LAVALINK_DIR"
if [ ! -f "$LAVALINK_DIR/Lavalink.jar" ] || [ "${1:-}" = "update" ]; then
    info "Downloading the latest Lavalink.jar..."
    curl -fL -o "$LAVALINK_DIR/Lavalink.jar" "$JAR_URL" || errx "Download failed."
else
    info "Lavalink.jar already present (use './setup-lavalink.sh update' to refresh)."
fi

# 3. Write application.yml (kept if it already exists, so manual edits survive).
if [ ! -f "$LAVALINK_DIR/application.yml" ]; then
    info "Writing application.yml..."
    cat > "$LAVALINK_DIR/application.yml" <<EOF
server:
  port: ${LAVALINK_PORT}
  address: 0.0.0.0
lavalink:
  plugins:
    # Native YouTube was deprecated in Lavalink v4; this plugin restores it.
    # If Lavalink fails to start on this version, bump it to one that exists at
    # https://github.com/lavalink-devs/youtube-source/releases
    - dependency: "dev.lavalink.youtube:youtube-plugin:${YOUTUBE_PLUGIN_VERSION}"
      snapshot: false
  server:
    password: "${LAVALINK_PASSWORD}"
    sources:
      youtube: false
      bandcamp: true
      soundcloud: true
      twitch: true
      vimeo: true
      http: true
      local: false
plugins:
  youtube:
    enabled: true
    allowSearch: true
logging:
  level:
    root: INFO
    lavalink: INFO
EOF
else
    info "application.yml already exists - keeping it (delete it to regenerate)."
fi

# 4. Tell the user how to wire the bot to this server.
cat <<EOF

[lavalink] Add this to config/bot.ini so the bot connects:

    [Lavalink]
    uri = http://localhost:${LAVALINK_PORT}
    password = ${LAVALINK_PASSWORD}

EOF

# 5. Run it / install a service / print instructions.
case "${1:-}" in
    start)
        info "Starting Lavalink in the foreground (Ctrl+C to stop)..."
        cd "$LAVALINK_DIR" && exec java -Xmx"${HEAP}" -jar Lavalink.jar
        ;;
    systemd)
        service="/etc/systemd/system/lavalink.service"
        info "Installing systemd service at ${service} (needs sudo)..."
        sudo tee "$service" >/dev/null <<EOF
[Unit]
Description=Lavalink audio server (Yasuho)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${LAVALINK_DIR}
ExecStart=$(command -v java) -Xmx${HEAP} -jar ${LAVALINK_DIR}/Lavalink.jar
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now lavalink
        info "Service installed and started. Follow logs with: sudo journalctl -u lavalink -f"
        ;;
    *)
        cat <<EOF
[lavalink] Ready. To run it:
  foreground (test) : cd "${LAVALINK_DIR}" && java -Xmx${HEAP} -jar Lavalink.jar
  background (screen): screen -dmS lavalink bash -c 'cd "${LAVALINK_DIR}" && java -Xmx${HEAP} -jar Lavalink.jar'
  persistent service : ./setup-lavalink.sh systemd   (installs a systemd unit, needs sudo)
EOF
        ;;
esac
