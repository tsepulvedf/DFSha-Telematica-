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

RUN pip install --no-cache-dir -e "."

RUN useradd --create-home --uid 1000 dfsha && chown -R dfsha:dfsha "/app"
USER dfsha

# ~/.dfsha guarda token y cwd. Montarlo como volumen conserva la sesion entre
# invocaciones de `docker compose run`.
ENV DFSHA_HOME="/home/dfsha/.dfsha"

ENTRYPOINT ["dfsha"]
CMD ["--help"]
