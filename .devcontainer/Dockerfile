FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libsndfile1 \
        portaudio19-dev \
        ffmpeg \
        libasound2-dev \
        libffi-dev \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspaces/sahara-healthcare-suite

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.txt
