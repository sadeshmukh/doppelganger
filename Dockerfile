FROM ghcr.io/astral-sh/uv:alpine

WORKDIR /app

COPY . .

RUN apk add --no-cache cairo-dev pango-dev && uv sync --frozen

CMD ["uv", "run", "main.py"]