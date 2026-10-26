FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY kurator ./kurator
COPY prompts ./prompts
COPY config.yaml ./
RUN pip install --no-cache-dir .

VOLUME ["/app/data"]
CMD ["python", "-m", "kurator.bot.main"]
