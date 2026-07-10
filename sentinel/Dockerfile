# genly-sentinel — FastAPI + Claude Code CLI headless + git/gh
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (headless con `claude -p`)
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /srv/sentinel
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# /data = Railway volume (clon del repo + sqlite sobreviven deploys)
ENV SENTINEL_WORKDIR=/data
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
