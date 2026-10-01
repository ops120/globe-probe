FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config.example.yaml /app/config.yaml
RUN pip install --no-cache-dir .

EXPOSE 8620
VOLUME ["/app/data"]

CMD ["gpm", "--config", "/app/config.yaml", "server"]
