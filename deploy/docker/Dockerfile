# Clip Watcher for a NAS / any Docker host (amd64 or arm64). Build from the repo root:
#   docker compose -f deploy/docker/compose.yaml up -d --build
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core libgl1 libegl1 libgles2 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# publish = YouTube Data API. The browser publisher (publish-browser, the desktop default) needs a real
# Chrome on a display, so the image uploads through the API: CLIPWATCHER_YOUTUBE_MODE=api below.
RUN pip install --no-cache-dir -e ".[judge,telegram,live,calibrate,publish]" streamlink==8.6.3
COPY config ./config
COPY prompts ./prompts
COPY assets ./assets
COPY scripts ./scripts
ADD https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite models/blaze_face_short_range.tflite
ENV CLIPWATCHER_DATA=/data/work CLIPWATCHER_STAGING=/data/staging HF_HOME=/data/hf DRY_RUN=1 PYTHONUNBUFFERED=1 \
    CLIPWATCHER_YOUTUBE_MODE=api
VOLUME ["/data"]
HEALTHCHECK --interval=2m --timeout=10s CMD python -m clipwatcher.health || exit 1
CMD ["python", "-m", "clipwatcher.live"]
