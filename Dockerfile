# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=deploy.docker_settings \
    FREEDOMSHOP_DATA_DIR=/var/lib/freedomshop
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libzbar0 postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 freedomshop \
    && useradd --uid 10001 --gid freedomshop --no-create-home --shell /usr/sbin/nologin freedomshop
WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock && pip check
COPY . .
RUN mkdir -p /var/lib/freedomshop/media /var/lib/freedomshop/uploads/fixes /var/lib/freedomshop/static \
    && chown -R freedomshop:freedomshop /var/lib/freedomshop \
    && chmod 755 /app/deploy/docker-entrypoint.sh
USER freedomshop
EXPOSE 8001
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8001", "--workers", "2", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-"]
