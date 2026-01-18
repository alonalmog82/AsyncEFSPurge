# Multi-stage build for optimal image size
FROM python:3.14-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install the application
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Final stage - minimal runtime image
FROM python:3.14-slim

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
LABEL org.opencontainers.image.title="AsyncEFSPurge"
LABEL org.opencontainers.image.description="High-performance async file purger for AWS EFS"
LABEL org.opencontainers.image.version="1.8.0"
LABEL org.opencontainers.image.authors="Alon Almog <alon.almog@rivery.io>"
LABEL org.opencontainers.image.licenses="MIT"


