# dashboard/Dockerfile — Nginx + Python API server
FROM python:3.11-slim

WORKDIR /app

# Install nginx + supervisor + openssl (for htpasswd)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx supervisor openssl && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Nginx config
COPY dashboard/nginx.conf /etc/nginx/sites-available/default
RUN rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default && \
    rm -f /usr/share/nginx/html/index.nginx-debian.html

# Supervisor config
COPY dashboard/supervisord.conf /etc/supervisor/conf.d/dashboard.conf

# Static HTML
COPY dashboard/index.html /usr/share/nginx/html/index.html

# API server
COPY dashboard/api.py .

# Entrypoint — generates .htpasswd from DASHBOARD_PASSWORD, then starts supervisord
COPY dashboard/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
