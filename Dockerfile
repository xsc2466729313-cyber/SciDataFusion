FROM python:3.12-slim AS runtime

ARG SCIDATA_INSTALL_TORCH=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --system scidata && useradd --system --gid scidata --create-home scidata

COPY pyproject.toml README.md ./
COPY src ./src
RUN if [ "$SCIDATA_INSTALL_TORCH" = "true" ]; then \
      python -m pip install ".[platform,scientific,ai-full]"; \
    else \
      python -m pip install ".[platform,scientific]"; \
    fi

RUN mkdir -p /app/var /app/config && chown -R scidata:scidata /app
USER scidata

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "scidatafusion.api:app", "--host", "0.0.0.0", "--port", "8000"]
