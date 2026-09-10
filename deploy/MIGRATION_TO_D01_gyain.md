# FundOS Backend — gyain enterprise reconfiguration (befundos, 10.130.0.34)

Move the existing `/opt/fundos` install to `/d01`, rebind services to the
private IP, point at the gyain hostnames, and use local `/d01` storage.

**Multitenancy note.** FundOS is already multitenant: the tenant is fixed at
login and carried in the JWT (`fundos/core/auth.py`), with row-level isolation
by `tenant_id`. It is never derived from the URL or subdomain. So there is
**one database and one `manage.py migrate`** for all tenants — nothing about
tenancy changes in this move.

Run everything below on **befundos (10.130.0.34)** as a sudo-capable user.

## 1. Hostname
```bash
sudo hostnamectl set-hostname befundos.gyain.com
echo '10.130.0.34  befundos.gyain.com befundos' | sudo tee -a /etc/hosts
```

## 2. Stop services
```bash
sudo systemctl stop fundos-web fundos-worker fundos-beat
```

## 3. Move the app tree to /d01
```bash
sudo mkdir -p /d01/fundos
sudo rsync -aHAX --info=progress2 /opt/fundos/ /d01/fundos/
sudo usermod --home /d01/fundos fundos
sudo chown -R fundos:fundos /d01/fundos
```

### 3a. Recreate the virtualenv (its paths are hard-coded to /opt)
```bash
sudo -u fundos rm -rf /d01/fundos/app/venv
sudo -u fundos python3.11 -m venv /d01/fundos/app/venv
sudo -u fundos /d01/fundos/app/venv/bin/pip install --upgrade pip wheel
sudo -u fundos /d01/fundos/app/venv/bin/pip install -r /d01/fundos/app/current/requirements.txt
sudo -u fundos /d01/fundos/app/venv/bin/pip install psycopg2-binary gunicorn
```

### 3b. OCR engine (Company Profile pipeline)

The profile pipeline reads scanned PDF pages and images embedded in decks.
`pytesseract` is a *binding*; the engine itself is a system package.

```bash
sudo dnf install -y tesseract          # or: apt install tesseract-ocr
tesseract --version                    # confirm it is on PATH
```

Without it the pipeline still runs: every file falls back to native text
extraction and each document row records `ocr_status=failed` with the reason
"No OCR engine is available on this host", so the gap is visible rather than
silent. Scanned documents simply contribute nothing.

If the binary is not on `PATH`, set `FUNDOS_TESSERACT_CMD` in
`/etc/fundos/fundos.env` to its full path. Which engine to use (and whether OCR
runs at all) is set in Django admin → Application Settings, not here.

## 4. Media / logs / backups on /d01
```bash
sudo mkdir -p /d01/fundos-data/media /d01/fundos-data/backups /var/log/fundos
sudo rsync -aHAX /srv/fundos/ /d01/fundos-data/ 2>/dev/null || true
sudo chown -R fundos:fundos /d01/fundos-data /var/log/fundos
sudo semanage fcontext -a -t var_t "/d01/fundos-data(/.*)?"
sudo restorecon -Rv /d01/fundos-data /d01/fundos
```

## 5. Environment file
```bash
sudo install -o root -g fundos -m 640 deploy/befundos.env.template /etc/fundos/fundos.env
sudo vi /etc/fundos/fundos.env   # replace every CHANGE_ME
```

## 6. Rebind data services to the private IP (10.130.0.34)
```bash
# PostgreSQL
sudo sed -i "s/^#*listen_addresses.*/listen_addresses = '10.130.0.34'/" /var/lib/pgsql/data/postgresql.conf
echo "host  fundos  fundos  10.130.0.34/32  scram-sha-256" | sudo tee -a /var/lib/pgsql/data/pg_hba.conf
sudo systemctl restart postgresql

# Redis
sudo sed -i "s/^bind .*/bind 10.130.0.34/" /etc/redis/redis.conf
sudo systemctl restart redis

# ClamAV
sudo sed -i "s/^TCPAddr .*/TCPAddr 10.130.0.34/" /etc/clamd.d/scan.conf
sudo systemctl restart clamd@scan
```

### 6a. Firewall — MANDATORY now (services left loopback isolation)
```bash
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --new-zone=fundos-data 2>/dev/null || true
sudo firewall-cmd --permanent --zone=fundos-data --add-source=10.130.0.34/32
sudo firewall-cmd --permanent --zone=fundos-data --add-port=5432/tcp
sudo firewall-cmd --permanent --zone=fundos-data --add-port=6379/tcp
sudo firewall-cmd --permanent --zone=fundos-data --add-port=3310/tcp
sudo firewall-cmd --reload
```

## 7. Systemd units + Nginx
```bash
sudo cp deploy/systemd/fundos-web.service    /etc/systemd/system/
sudo cp deploy/systemd/fundos-worker.service /etc/systemd/system/
sudo cp deploy/systemd/fundos-beat.service   /etc/systemd/system/
sudo systemctl daemon-reload

# Wildcard cert
sudo mkdir -p /etc/pki/gyain
sudo cp /tmp/wildcard.gyain.com.fullchain.pem /etc/pki/gyain/fullchain.pem
sudo cp /tmp/wildcard.gyain.com.privkey.pem   /etc/pki/gyain/privkey.pem
sudo chmod 600 /etc/pki/gyain/privkey.pem

sudo cp deploy/nginx/fundos-api.conf /etc/nginx/conf.d/fundos-api.conf
sudo setsebool -P httpd_can_network_connect 1
sudo semanage port -a -t http_port_t -p tcp 8001 2>/dev/null || sudo semanage port -m -t http_port_t -p tcp 8001
sudo nginx -t && sudo systemctl enable --now nginx
```

## 8. Migrate against the private-IP DB, then start
```bash
cd /d01/fundos/app/current
set -a; source /etc/fundos/fundos.env; set +a
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py check --deploy
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py migrate

# Silk 2.0 — REQUIRED on every deploy, including upgrades.
# Countries, the seven fund-raise bands, traction rules, stage definitions,
# profile sections, prompts and UI copy live in configuration tables. The
# app degrades honestly without them (endpoints return 200 with empty
# lists, and the fund-raise step reports "not enough information" rather
# than guessing) but no recommendation can be produced until this has run.
# It is idempotent: existing rows are left as-is, so it is safe to repeat.
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py seed_platform_config

# C7 — links artefacts generated under the old Stage 3 into the Company
# Profile Document Center, read-only. Run ONCE on an upgraded database.
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py migrate_to_silk

sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py collectstatic --noinput

# Django admin is mounted at /admin/. It needs a superuser and the static
# files collected above; see "Web tier — /admin and /static" below for the
# two nginx blocks required to reach it.
sudo -E -u fundos /d01/fundos/app/venv/bin/python manage.py createsuperuser

sudo systemctl enable --now fundos-web fundos-worker fundos-beat
curl -s -o /dev/null -w "%{http_code}\n" http://10.130.0.34:8001/api/v1/me/contexts   # expect 401
```

## 9. Retire the old tree (after a clean run)
```bash
sudo mv /opt/fundos /opt/fundos.OLD    # keep as a safety net, remove later
```

## Log files to watch
| Stream | Command |
|---|---|
| Gunicorn access | `sudo tail -f /var/log/fundos/gunicorn-access.log` |
| Gunicorn error  | `sudo tail -f /var/log/fundos/gunicorn-error.log` |
| Celery worker (background jobs) | `sudo tail -f /var/log/fundos/celery-worker.log`  •  `journalctl -u fundos-worker -f` |
| Celery beat (scheduler)         | `sudo tail -f /var/log/fundos/celery-beat.log`    •  `journalctl -u fundos-beat -f` |
| Web service (systemd)           | `journalctl -u fundos-web -f` |
| PostgreSQL | `sudo tail -f /var/lib/pgsql/data/log/*.log` |
| Redis      | `journalctl -u redis -f` |
| ClamAV     | `journalctl -u clamd@scan -f` |
| Nginx      | `sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log` |


## Web tier — /admin and /static (QA 24-Jul issue 9)

`/admin` was unreachable for three separate reasons. Two were code defects
and are fixed in this release: no admin URL was mounted, and `STATIC_URL`
was relative so the admin's own CSS resolved against the wrong path. The
third is a web-tier configuration change that must be applied here.

**1. `location /admin/` does not match `/admin`.** nginx prefix locations
match on the literal string, so a request for `/admin` (no trailing slash)
skipped the proxy block, fell through to the SPA fallback and was answered
with `index.html` — which is the blank/error page the tester saw. Add an
exact-match block alongside the existing one:

```nginx
# Exact /admin — hand it to the same upstream so Django can issue its
# canonical redirect to /admin/.
location = /admin {
    proxy_pass https://befundos_upstream;
    proxy_ssl_server_name on;
    proxy_ssl_name befundos.gyain.com;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-Host  $host;
}
```

**2. `/static/` is not served.** The admin's CSS and JS live under
`STATIC_URL`. Without a location block the SPA fallback answers those
requests with `index.html` and the admin renders unstyled. Serve the
collected files directly from the web tier:

```nginx
location /static/ {
    alias /d01/fundos/app/current/staticfiles/;
    expires 1h;
    add_header Cache-Control "public";
}
```

If the web and app tiers are separate hosts, either rsync `staticfiles/`
to the web tier after `collectstatic`, or proxy `/static/` to the backend
in the same way as `/admin/`.

**3. Restrict who can reach it.** The admin is a superuser surface on the
same public hostname as the founder-facing app. Allowlist it:

```nginx
location /admin/ {
    allow 10.130.0.0/24;      # office / VPN range
    deny  all;
    # ... existing proxy_pass block ...
}
```

### Verifying
```bash
curl -sI https://fundos.gyain.com/admin  | head -1   # expect 301
curl -sI https://fundos.gyain.com/admin/ | head -1   # expect 302 -> /admin/login/
curl -sI https://fundos.gyain.com/static/admin/css/base.css | head -1   # expect 200
```

## Web tier — product logos

The two product marks ship inside the frontend build at
`/brand/silk-logo.svg` and `/brand/silk-logo-white.svg`, so they are served
by the existing SPA root and need no configuration. An administrator can
override them with hosted URLs in **Brand configuration** at any time; the
shipped files are only the default.
