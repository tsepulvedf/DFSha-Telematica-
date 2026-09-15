# ControlNode: metadatos del sistema de archivos. Los bytes no pasan por aqui.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Las dependencias primero: cambiar el codigo no vuelve a instalarlas.
COPY "pyproject.toml" "README.md" "./"
COPY "src" "./src"

RUN pip install --no-cache-dir -e "."

# La base de datos vive en un volumen, no en la capa de imagen.
RUN mkdir -p "/var/lib/dfsha-meta"
ENV DFSHA_DB_URL="sqlite:////var/lib/dfsha-meta/dfsha.db"

# Sin usuario root: el proceso no necesita privilegios para servir HTTP.
RUN useradd --create-home --uid 1000 dfsha \
    && chown -R dfsha:dfsha "/var/lib/dfsha-meta" "/app"
USER dfsha

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8000/health', timeout=3).status_code==200 else 1)"

# --factory: la app se construye al arrancar, no al importar el modulo, para que la
# falta de un secreto falle con un mensaje claro en vez de a mitad de la importacion.
CMD ["uvicorn", "dfsha.control_node.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
