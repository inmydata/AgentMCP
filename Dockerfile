FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN pip install --upgrade pip && \
    pip install uv && \
    uv sync --frozen

COPY server_remote.py ./

EXPOSE 8000

ENV TRANSPORT=sse
ENV PORT=8000

CMD ["sh", "-c", "uv run python server_remote.py $TRANSPORT $PORT"]
