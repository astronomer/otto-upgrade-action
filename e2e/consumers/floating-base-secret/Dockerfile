FROM astrocrpublic.azurecr.io/runtime:3.3-base
LABEL io.astronomer.docker=true
LABEL io.astronomer.docker.airflow.onbuild=true
COPY packages.txt .
USER root
RUN /usr/local/bin/install-system-packages
COPY requirements.txt .
RUN --mount=type=secret,id=netrc,target=/root/.netrc,required=false \
      /usr/local/bin/install-python-dependencies
USER astro
COPY --chown=astro:0 . .
