# Cliente CLI. Util para correr el GC o la CLI sin instalar Python en la maquina.
#
# OJO con la URL del DataNode: dentro de la red de compose hay que hablarle por su
# nombre de servicio, y para eso el DataNode tiene que haberse anunciado como
# http://data-node-1:8001. El quickstart del README usa el cliente del host, que es
# el escenario soportado en la Etapa 1. Ver DFSHA_DATANODE_BASE_URL en .env.example.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY "pyproject.toml" "README.md" "./"
COPY "src" "./src"
COPY "scripts" "./scripts"

RUN pip install --no-cache-dir -e "." "grpcio-tools>=1.60"

# El codigo del .proto no se versiona: se genera aqui, contra el .proto de
# esta imagen, de modo que no puede quedar desincronizado.
COPY "scripts/gen_proto.py" "./scripts/gen_proto.py"
RUN python scripts/gen_proto.py

# ~/.dfsha guarda token y cwd. Montarlo como volumen conserva la sesion entre
# invocaciones de `docker compose run`.
#
# El directorio TIENE que existir en la imagen antes del montaje: Docker crea los
# puntos de montaje que faltan como root, y entonces el cliente no podria escribir
# session.json y `dfsha login` fallaria con un error de permisos. Cuando el
# directorio ya existe, el volumen nombrado hereda su dueno y sus permisos.
ENV DFSHA_HOME="/home/dfsha/.dfsha"

RUN useradd --create-home --uid 1000 dfsha \
    && mkdir -p "/home/dfsha/.dfsha" \
    && chown -R dfsha:dfsha "/home/dfsha" "/app"
USER dfsha

ENTRYPOINT ["dfsha"]
CMD ["--help"]
