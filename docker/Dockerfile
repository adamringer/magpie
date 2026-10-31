# Buildroot build environment for the magpie firmware.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        which sed make binutils build-essential diffutils gcc g++ bash patch \
        gzip bzip2 perl tar cpio unzip rsync file bc findutils wget git \
        python3 libncurses-dev ca-certificates xz-utils locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Non-root user matching the host user, so files in the mounted repo stay
# owned by the host user. The base image ships an "ubuntu" user at UID 1000.
ARG UID=1000
ARG GID=1000
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && (getent group ${GID} >/dev/null || groupadd -g ${GID} builder) \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash builder

USER builder
WORKDIR /work
