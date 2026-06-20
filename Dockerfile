# Build/test environment for hpgl-buddy.
#
# This image is for building the wheel and running the test suite (CI, local
# reproducible builds). It is NOT for talking to the plotter: serial hardware
# is not reachable from a container on macOS/Windows hosts.
#
#   docker build -t hpgl-buddy-build .
#   docker run --rm hpgl-buddy-build              # runs the test suite (default)
#   docker run --rm hpgl-buddy-build tox -e build # builds the wheel into /app/dist

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN python -m pip install --upgrade pip

# Install the pinned runtime + dev (test/build) tooling first, for a
# reproducible, well-cached layer.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

# Then the package itself, without re-resolving deps (they are pinned above).
COPY . .
RUN pip install -e . --no-deps

# Default to the canonical task runner; override with `tox -e build`, etc.
CMD ["tox"]
