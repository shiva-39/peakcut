# PeakCut Production Deployment Guide

This guide covers deploying PeakCut 24/7 on a Linux VPS (Ubuntu 22.04/24.04) using either Docker Compose (recommended) or a native systemd service.

---

## Option 1: Docker Compose (Recommended)

### Prerequisites

- Ubuntu 22.04+ VPS with 2+ GB RAM, 2+ vCPU
- A domain name pointing to the VPS (for HTTPS/OAuth)
- Docker Engine 24+ and Docker Compose v2+

### Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/peakcut.git
cd peakcut

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your settings (see required variables below)

# 3. Deploy
docker compose up -d --build

# 4. Verify
docker compose logs -f app
```

### Required Environment Variables

Create `.env` with at least:

```bash
# Domain (must have DNS A record pointing to this VPS)
PEAKCUT_DOMAIN=peakcut.yourdomain.com

# Database (auto-generated if not set)
POSTGRES_PASSWORD=your-secure-random-password

# Google OAuth (for YouTube upload)
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
# Authorized redirect URIs in Google Cloud Console:
#   https://peakcut.yourdomain.com/auth/google/callback
#   https://peakcut.yourdomain.com/oauth/youtube/callback

# AI Provider (at least one)
PEAKCUT_AI_PROVIDER=gemini
PEAKCUT_AI_KEY=your-ai-api-key

# Optional: resource limits
PEAKCUT_MEM_LIMIT=4g
PEAKCUT_CPUS=2.0
```

### Production Profiles

**Own HTTPS (Caddy manages certificates):**
```bash
# Add to .env
COMPOSE_PROFILES=own-https
```

**Behind existing reverse proxy (Nginx/Traefik):**
```bash
# Don't set COMPOSE_PROFILES; use deploy/nginx-peakcut.conf or deploy/docker-compose.traefik.yml
```

### Useful Commands

```bash
# View logs
docker compose logs -f app

# Restart
docker compose restart app

# Update to latest
git pull && docker compose up -d --build

# Backup data
docker compose exec db pg_dump -U peakcut peakcut > backup.sql
docker run --rm -v peakcut_data:/data -v $(pwd):/backup alpine tar czf /backup/data-backup.tar.gz -C /data .
```

---

## Option 2: Native systemd Service (No Docker)

### Prerequisites

- Ubuntu 22.04+
- Python 3.11+
- PostgreSQL 16+
- ffmpeg
- 2+ GB RAM

### Installation

```bash
# 1. Create user and directories
sudo useradd -r -s /bin/bash -d /opt/peakcut peakcut
sudo mkdir -p /opt/peakcut /opt/peakcut/data /opt/peakcut/secrets
sudo chown -R peakcut:peakcut /opt/peakcut

# 2. Clone and setup
cd /opt/peakcut
sudo -u peakcut git clone https://github.com/yourusername/peakcut.git .
sudo -u peakcut python3 -m venv .venv
sudo -u peakcut .venv/bin/pip install --upgrade pip
sudo -u peakcut .venv/bin/pip install -r requirements.txt

# 3. Install system dependencies
sudo apt-get update && sudo apt-get install -y ffmpeg postgresql-client

# 4. Configure PostgreSQL
sudo -u postgres psql -c "CREATE USER peakcut WITH PASSWORD 'your-password';"
sudo -u postgres psql -c "CREATE DATABASE peakcut OWNER peakcut;"

# 5. Configure environment
sudo cp .env.example /opt/peakcut/.env
# Edit /opt/peakcut/.env with your settings
sudo chown peakcut:peakcut /opt/peakcut/.env
sudo chmod 600 /opt/peakcut/.env

# 6. Install systemd service
sudo cp deploy/peakcut.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peakcut

# 7. Verify
sudo journalctl -u peakcut -f
```

### Required .env for systemd

```bash
DATABASE_URL=postgresql+psycopg://peakcut:your-password@localhost:5432/peakcut
PEAKCUT_PUBLIC_URL=https://peakcut.yourdomain.com
PEAKCUT_HOST=0.0.0.0
PEAKCUT_PORT=8765
PEAKCUT_ACCOUNTS=1
PEAKCUT_TRUST_PROXY=1

# Google OAuth
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# AI
PEAKCUT_AI_PROVIDER=gemini
PEAKCUT_AI_KEY=...
```

### Nginx Reverse Proxy (Required for HTTPS/OAuth)

```nginx
# /etc/nginx/sites-available/peakcut
server {
    listen 80;
    server_name peakcut.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name peakcut.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/peakcut.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/peakcut.yourdomain.com/privkey.pem;

    # Large uploads
    client_max_body_size 2.2G;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Long timeouts for uploads
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/peakcut /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d peakcut.yourdomain.com
```

---

## YouTube OAuth Configuration

**Google Cloud Console → APIs & Services → Credentials:**

1. Create OAuth 2.0 Client ID (Web application)
2. Authorized JavaScript origins:
   - `https://peakcut.yourdomain.com`
3. Authorized redirect URIs:
   - `https://peakcut.yourdomain.com/auth/google/callback`
   - `https://peakcut.yourdomain.com/oauth/youtube/callback`

**In PeakCut Settings → Connected Apps → YouTube:**
- Paste Client ID and Client Secret
- Click "Connect YouTube" and authorize

---

## Quota & Scheduling

PeakCut respects YouTube's daily upload quota (default 6 Shorts/day, resets at **midnight Pacific Time**).

Configure in **Settings → Publishing**:
- **Daily upload limit**: 1–100 (default 6)
- **Minimum interval**: Minutes between uploads (default 120 for Shorts drip-feed)
- **Default privacy**: `private` (recommended for unverified OAuth projects), `unlisted`, or `public`

---

## Monitoring & Maintenance

```bash
# Health check
curl https://peakcut.yourdomain.com/healthz

# View app logs (Docker)
docker compose logs -f app --tail=100

# View app logs (systemd)
sudo journalctl -u peakcut -f

# Database backup
docker compose exec db pg_dump -U peakcut peakcut | gzip > backup-$(date +%F).sql.gz

# Data backup (Docker)
docker run --rm -v peakcut_data:/data -v $(pwd):/backup alpine tar czf /backup/data-$(date +%F).tar.gz -C /data .
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| OAuth redirect mismatch | Ensure `PEAKCUT_PUBLIC_URL` matches your domain exactly (including `https://`) |
| Uploads fail at 100MB | Increase `client_max_body_size` in Nginx / Caddyfile |
| Clips not rendering | Check `ffmpeg` is installed; verify `PEAKCUT_DATA` volume has space |
| YouTube quota errors | Reduce daily limit; quota resets at midnight PT |
| AI not working | Verify `PEAKCUT_AI_KEY` and provider in Settings → AI |

---

## Security Checklist

- [ ] Strong `POSTGRES_PASSWORD` in `.env`
- [ ] `PEAKCUT_ACCOUNTS=1` for multi-user servers
- [ ] HTTPS enforced (Certbot/Caddy)
- [ ] `.env` file permissions `600`
- [ ] Firewall: only 80/443 open (SSH on separate port)
- [ ] Regular `git pull && docker compose up -d --build` for updates

---

## Architecture Overview

```
                    ┌─────────────┐
                    │   Client    │
                    └──────┬──────┘
                           │ HTTPS
                           ▼
                    ┌─────────────┐
                    │  Caddy/Nginx │  (TLS termination, large uploads)
                    └──────┬──────┘
                           │ HTTP
                           ▼
                    ┌─────────────┐
                    │   PeakCut   │  (FastAPI + background workers)
                    │  (port 8765) │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌──────────┐    ┌────────────┐    ┌──────────┐
    │PostgreSQL│    │  Local FS  │    │ YouTube  │
    │ (metadata)│    │ (/data)    │    │   API    │
    └──────────┘    └────────────┘    └──────────┘
```

---

## Support

- GitHub Issues: https://github.com/yourusername/peakcut/issues
- Documentation: See `AGENTS.md` in the repository root