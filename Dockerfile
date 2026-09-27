# DOGFOOD portal image.
#
# Building pulls the Python base image and the pinned wheels: the only step that needs the
# internet. The running container makes no outbound connection of any kind.

FROM python:3.12-slim

# Single-threaded BLAS (OPENBLAS/OMP): scoring results must not depend on thread timing.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    PYTHONPATH=/app/src \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

WORKDIR /app

RUN groupadd --system --gid 1001 dogfood \
    && useradd --system --uid 1001 --gid dogfood --create-home dogfood

# Dependencies first, so editing code does not invalidate the wheel layer.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

COPY --chown=dogfood:dogfood src/ /app/src/
COPY --chown=dogfood:dogfood tests/ /app/tests/
COPY --chown=dogfood:dogfood pytest.ini /app/pytest.ini
COPY --chown=dogfood:dogfood docker/entrypoint.sh /app/docker/entrypoint.sh

# Strip Windows line endings: a CRLF entrypoint fails with "/bin/sh^M: bad interpreter".
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh && chmod +x /app/docker/entrypoint.sh

# Static files are collected at build time, so boot stays fast. The key is a throwaway:
# nothing is signed during collectstatic.
RUN DJANGO_SECRET_KEY=build-only DJANGO_STATIC_ROOT=/app/staticfiles \
    python /app/src/manage.py collectstatic --noinput -v0 \
    && mkdir -p /app/media/projects \
    && chown -R dogfood:dogfood /app/staticfiles /app/media

USER dogfood
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", "--timeout", "60", \
     "--access-logfile", "-", "--error-logfile", "-"]
