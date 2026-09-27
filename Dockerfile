# DOGFOOD 2026 portal image.
#
# Network use: building this image pulls the python base image and the pinned wheels from
# PyPI. That is the only step that needs the internet. Once built, the container makes no
# outbound connection of any kind -- no CDN, no font host, no API, no mail server. The
# README states this plainly rather than claiming an offline build.

FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: no .pyc litter in the image or on the mounted volume.
# - PYTHONUNBUFFERED: the entrypoint prints demo credentials, and a buffered stdout would
#   hold them back until the buffer flushed, which looks like a hang to whoever ran
#   `docker compose up`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    PYTHONPATH=/app/src

WORKDIR /app

# psycopg is installed as psycopg[binary], so no libpq build chain is needed here; the
# image stays slim and the build stays fast. `postgresql-client` is not installed either:
# the entrypoint waits for the database through Python, using the same driver and
# credentials the application itself will use, which is a stronger readiness signal than
# pg_isready and one less package to keep patched.
RUN groupadd --system --gid 1001 dogfood \
    && useradd --system --uid 1001 --gid dogfood --create-home dogfood

# Dependencies first, so editing application code does not invalidate the wheel layer.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

COPY --chown=dogfood:dogfood src/ /app/src/
COPY --chown=dogfood:dogfood docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

# Static files are collected at build time, not at boot: a container that has to collect
# static on every start is slower to become healthy and can fail at the worst moment.
# SECRET_KEY is supplied only so the settings module imports; nothing is signed here and
# this value never reaches the running container, which reads its own env.
RUN DJANGO_SECRET_KEY=build-time-only-not-a-runtime-secret \
    DJANGO_STATIC_ROOT=/app/staticfiles \
    python /app/src/manage.py collectstatic --noinput --clear

# Writable at runtime: uploaded images (a named volume is mounted here by compose) and the
# collected static tree. Created before dropping privileges so the non-root user owns them.
RUN mkdir -p /app/media \
    && chown -R dogfood:dogfood /app/media /app/staticfiles

USER dogfood

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Two workers: enough to show that nothing in the design depends on a single process (the
# login throttle counts rows in Postgres, not in per-process memory), while staying modest
# on a laptop. Gunicorn's own timeout is generous enough for a large gallery page.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
