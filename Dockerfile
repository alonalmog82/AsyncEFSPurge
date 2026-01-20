# Multi-stage build for optimal image size
FROM python:3.14-slim as builder

WORKDIR /build

# Set non-interactive frontend for apt to suppress debconf warnings
ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Extract version from pyproject.toml and save it for later stages
RUN python3 -c "import tomllib; f=open('pyproject.toml','rb'); d=tomllib.load(f); print(d['project']['version'])" > /build/VERSION

# Install the application
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Final stage - minimal runtime image
FROM python:3.14-slim

# ARG for version (passed from CI/CD or extracted from pyproject.toml in builder stage)
# For local builds: docker build --build-arg VERSION=$(python3 -c "import tomllib; f=open('pyproject.toml','rb'); d=tomllib.load(f); print(d['project']['version'])")
ARG VERSION

# Copy version file from builder (fallback if VERSION arg not provided)
COPY --from=builder /build/VERSION /tmp/VERSION

# Set VERSION from file if not provided as build arg (for local builds)
RUN if [ -z "$VERSION" ]; then export VERSION=$(cat /tmp/VERSION); fi

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash --uid 1000 efspurge

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/efspurge /usr/local/bin/efspurge

# Switch to non-root user
USER efspurge

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default command shows help
ENTRYPOINT ["efspurge"]
CMD ["--help"]

# Labels for metadata
# Version is passed as ARG VERSION (extracted from pyproject.toml in CI/CD or builder stage)
LABEL org.opencontainers.image.title="AsyncEFSPurge"
LABEL org.opencontainers.image.description="High-performance async file purger for AWS EFS"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.authors="Alon Almog <alon.almog@rivery.io>"
LABEL org.opencontainers.image.licenses="MIT"


