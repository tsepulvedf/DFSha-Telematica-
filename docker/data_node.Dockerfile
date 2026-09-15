# DataNode: almacena bloques opacos por block_id. No conoce rutas ni usuarios.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY "pyproject.toml" "README.md" "./"
COPY "src" "./src"

RUN pip install --no-cache-dir -e "." "grpcio-tools>=1.60"

# El codigo del .proto no se versiona: se genera aqui, contra el .proto de
# esta imagen, de modo que no puede quedar desincronizado.
COPY "scripts/gen_proto.py" "./scripts/gen_proto.py"
RUN python scripts/gen_proto.py

ENV DFSHA_DATA_DIR="/var/lib/dfsha"
RUN mkdir -p "/var/lib/dfsha"

RUN useradd --create-home --uid 1000 dfsha \
    && chown -R dfsha:dfsha "/var/lib/dfsha" "/app"
USER dfsha

EXPOSE 8001

# Intervalo mas largo que el del ControlNode a proposito: /health recorre el disco
# para contar bloques y bytes, en vez de fiarse de un contador en memoria.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8001/health', timeout=3).status_code==200 else 1)"

CMD ["uvicorn", "dfsha.data_node.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8001"]
