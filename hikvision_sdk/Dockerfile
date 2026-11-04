# The base image is pinned here on purpose. The Hikvision SDK is a glibc binary, and
# Supervisor's default BUILD_FROM (an Alpine/musl image) would not load it.
FROM python:3.13-slim-bookworm

ARG BUILD_ARCH
WORKDIR /app

COPY fetch_sdk.py sdk-files.sha256 /build/
RUN python3 /build/fetch_sdk.py "${BUILD_ARCH}" /app/sdk && rm -rf /build

COPY src/hik_sdk_bridge /app/hik_sdk_bridge

ENV LD_LIBRARY_PATH=/app/sdk:/app/sdk/HCNetSDKCom \
    HIK_SDK_LIB_DIR=/app/sdk \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "-m", "hik_sdk_bridge"]
