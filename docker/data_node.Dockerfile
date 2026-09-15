# DataNode: almacena bloques opacos por block_id. No conoce rutas ni usuarios.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY "pyproject.toml" "README.md" "./"
COPY "src" "./src"

RUN pip install --no-cache-dir -e "."

ENV DFSHA_DATA_DIR="/var/lib/dfsha"
RUN mkdir -p "/var/lib/dfsha"

RUN useradd --create-home --uid 1000 dfsha \
    && chown -R dfsha:dfsha "/var/lib/dfsha" "/app"
USER dfsha

EXPOSE 8001

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8001/health', timeout=3).status_code==200 else 1)"

CMD ["uvicorn", "dfsha.data_node.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8001"]
