FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 vscode \
    && echo "vscode ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/vscode

USER vscode
