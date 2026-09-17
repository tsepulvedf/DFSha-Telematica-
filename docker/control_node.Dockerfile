# ControlNode: metadatos del sistema de archivos. Los bytes no pasan por aqui.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Las dependencias primero: cambiar el codigo no vuelve a instalarlas.
COPY "pyproject.toml" "README.md" "./"
COPY "src" "./src"

# Las migraciones viajan en la imagen por dos motivos: el servicio `migrate` del compose
# las aplica con esta misma imagen, y el ControlNode necesita los scripts para comprobar
# al arrancar que la base esta en la ultima revision.
COPY "alembic.ini" "./"
COPY "alembic" "./alembic"

RUN pip install --no-cache-dir -e "." "grpcio-tools>=1.60"

# El codigo del .proto no se versiona: se genera aqui, contra el .proto de
# esta imagen, de modo que no puede quedar desincronizado.
COPY "scripts/gen_proto.py" "./scripts/gen_proto.py"
RUN python scripts/gen_proto.py

# Sin DFSHA_DB_URL por defecto: desde la Etapa 3 el metadato es PostgreSQL y vive
# fuera de esta imagen. Un default de SQLite aqui haria que un despliegue al que se le
# olvido la variable arrancara feliz contra un fichero dentro del contenedor, y
# perdiera el metadato entero al reiniciarlo.

# Sin usuario root: el proceso no necesita privilegios para servir HTTP.
RUN useradd --create-home --uid 1000 dfsha \
    && chown -R dfsha:dfsha "/app"
USER dfsha

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8000/health', timeout=3).status_code==200 else 1)"

# --factory: la app se construye al arrancar, no al importar el modulo, para que la
# falta de un secreto falle con un mensaje claro en vez de a mitad de la importacion.
# Se arranca por el modulo y no por `uvicorn` directamente porque el TLS de cliente es
# OPCIONAL: un CMD con las banderas puestas obligaria a que todo despliegue tuviera
# certificados de cliente, y sin ellas no habria forma de activarlo. La decision la toma
# la configuracion. Ver src/dfsha/common/serve.py.
CMD ["python", "-m", "dfsha.control_node"]
