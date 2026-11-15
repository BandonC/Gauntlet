# Image for the `demo` compose profile: API, worker and one-shot jobs, all running `arena`.
# Editable on purpose: the migration runner finds migrations/ beside the package, in /app.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY arena ./arena
COPY migrations ./migrations
COPY scripts/demo_seed.py ./scripts/demo_seed.py
RUN pip install --no-cache-dir -e .
