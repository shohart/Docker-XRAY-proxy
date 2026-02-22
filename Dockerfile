# Xray Core Dockerfile for XRAY-PROXY-Container
FROM ghcr.io/xtls/xray-core:latest

# Set working directory
WORKDIR /etc/xray

# Copy configuration files
COPY config/ ./

# Expose ports for HTTP and SOCKS5 proxies
EXPOSE 3128 1080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD /usr/bin/xray run -test -c /etc/xray/config.json

# Default command
CMD ["/usr/bin/xray", "run", "-c", "/etc/xray/config.json"]
