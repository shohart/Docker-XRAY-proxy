# Xray Core Dockerfile for XRAY-PROXY-Container
FROM ghcr.io/xtls/xray:latest

# Set working directory
WORKDIR /app

# Copy configuration files
COPY config/ ./config/
COPY scripts/ ./scripts/

# Make script executable
RUN chmod +x ./scripts/update_subscription.sh

# Expose ports for HTTP and SOCKS5 proxies
EXPOSE 3128 1080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:3128/ || exit 1

# Default command
CMD ["/usr/bin/xray", "run", "-c", "/app/config/config.json"]