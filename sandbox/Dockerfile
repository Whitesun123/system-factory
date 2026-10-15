# System Factory execution sandbox.
# Base image pinned by digest; test dependencies pinned by version and sha256 hash.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -c "import pytest; print('pytest', pytest.__version__)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INDEX=1

# Unprivileged user (nobody). The runtime also passes --user 65534:65534 explicitly.
USER 65534:65534
WORKDIR /work
