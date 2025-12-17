# syntax=docker/dockerfile:1.4
FROM --platform=$BUILDPLATFORM python:3.11-slim-bookworm AS base

ARG PYTHON_PACKAGES_PATH=/usr/local/lib/python3.11/site-packages

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential python3-dev libffi-dev \
      libjpeg-dev zlib1g-dev libtiff-dev \
      libfreetype6-dev libwebp-dev libopenjp2-7-dev \
      libgomp1 \
      ffmpeg libgl1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /printguard
COPY printguard/requirements.txt requirements.txt
COPY pyproject.toml .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && apt-get remove -y build-essential python3-dev libffi-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
COPY . /printguard

RUN pip install .

FROM --platform=$TARGETPLATFORM python:3.11-slim-bookworm AS runtime

COPY --from=base /usr/local /usr/local

WORKDIR /printguard
COPY --from=base /printguard /printguard

EXPOSE 8000
VOLUME ["/data"]
ENTRYPOINT ["printguard"]