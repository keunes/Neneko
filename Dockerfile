# syntax=docker/dockerfile:1

# kepubify, built from source through the Go module proxy (checksums verified by sum.golang.org)
FROM --platform=$BUILDPLATFORM golang:1.24-bookworm AS kepubify
ARG TARGETOS TARGETARCH TARGETVARIANT
ARG KEPUBIFY_VERSION=v4.0.4
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOARM=${TARGETVARIANT#v} \
    go install -trimpath -ldflags "-s -w -X main.version=${KEPUBIFY_VERSION}" \
        github.com/pgaskin/kepubify/v4/cmd/kepubify@${KEPUBIFY_VERSION} \
 && mkdir -p /out && cp "$(find /go/bin -type f -name kepubify | head -n 1)" /out/kepubify

FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
ENV PIP_ROOT_USER_ACTION=ignore
RUN pip install --no-cache-dir . && rm -rf /app/src /app/build
COPY --from=kepubify /out/kepubify /usr/local/bin/kepubify
RUN useradd --uid 1000 --no-create-home --home-dir /data neneko \
 && mkdir -p /data && chown neneko:neneko /data
USER neneko
ENV NENEKO_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8089
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8089/healthz', timeout=3)"
CMD ["neneko"]
