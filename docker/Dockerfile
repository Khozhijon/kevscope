# Linux x86-64 environment for the QNX SDP 8.0 host tools on an Apple-silicon Mac.
# The SDP itself is NOT in this image: it is mounted from the host at run time,
# so the image contains no QNX software and can be rebuilt by anyone.
FROM --platform=linux/amd64 ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates make file xz-utils unzip bzip2 rsync \
      python3 openssh-client less locales libxml2 \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8
WORKDIR /work
CMD ["bash"]
