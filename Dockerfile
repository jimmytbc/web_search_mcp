FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY . .
EXPOSE 8000
CMD ["uv", "run", "python", "server.py"]
