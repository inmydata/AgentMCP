# BASE_IMAGE defaults to Docker Hub for local builds. CI overrides it with the
# ECR pull-through cache path (see buildspec.yml) so builds don't hit Docker Hub
# rate limits.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN pip install --upgrade pip && \
    pip install uv && \
    uv sync --frozen

# Copy all Python modules
COPY *.py ./

EXPOSE 8000

ENV TRANSPORT=streamable-http
ENV PORT=8000

CMD ["sh", "-c", "uv run python server_remote.py $TRANSPORT $PORT"]
