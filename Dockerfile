FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN useradd --create-home monitor && chown -R monitor:monitor /app
USER monitor
CMD ["job-monitor", "bot"]
