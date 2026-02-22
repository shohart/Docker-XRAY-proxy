#!/bin/bash

# Script to update Xray subscription and reload configuration
# This script should be run periodically by the updater service

set -e  # Exit on any error

echo "[INFO] Updating subscription at $(date)" >> /var/log/xray/updater.log

# Check if required environment variables are set
if [ -z "$XRAY_SUBSCRIPTION_URL" ]; then
    echo "[ERROR] XRAY_SUBSCRIPTION_URL is not set" >> /var/log/xray/updater.log
    exit 1
fi

if [ -z "$HTTP_PROXY_PORT" ]; then
    HTTP_PROXY_PORT=3128
fi

if [ -z "$SOCKS_PROXY_PORT" ]; then
    SOCKS_PROXY_PORT=1080
fi

# Download subscription
echo "[INFO] Downloading subscription from $XRAY_SUBSCRIPTION_URL" >> /var/log/xray/updater.log
curl -s -o /etc/xray/subscription.json "$XRAY_SUBSCRIPTION_URL"

if [ $? -eq 0 ]; then
    echo "[INFO] Subscription updated successfully" >> /var/log/xray/updater.log
    
    # Parse subscription and generate Xray configuration
    # This is a simplified version - in practice, you might need more complex parsing
    # based on the specific subscription format provided by your provider
    
    # Create a basic Xray config that can be used with subscription data
    cat > /etc/xray/config.json << EOF
{
  "log": {
    "access": "/var/log/xray/access.log",
    "error": "/var/log/xray/error.log",
    "loglevel": "info"
  },
  "inbounds": [
    {
      "port": $HTTP_PROXY_PORT,
      "protocol": "http",
      "settings": {
        "users": []
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls"]
      }
    },
    {
      "port": $SOCKS_PROXY_PORT,
      "protocol": "socks",
      "settings": {
        "auth": "noauth",
        "udp": true
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls"]
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "vmess",
      "settings": {
        "vnext": [
          {
            "address": "127.0.0.1",
            "port": 10086,
            "users": []
          }
        ]
      },
      "streamSettings": {
        "network": "tcp"
      }
    },
    {
      "protocol": "freedom",
      "settings": {}
    }
  ],
  "routing": {
    "domainStrategy": "IPOnDemand",
    "rules": [
      {
        "type": "field",
        "ip": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
        "outboundTag": "direct"
      }
    ]
  }
}
EOF

    echo "[INFO] Configuration reloaded successfully" >> /var/log/xray/updater.log
    
    # In a real implementation, you would restart or reload Xray here
    # For example: docker exec xray xray reload
else
    echo "[ERROR] Failed to update subscription" >> /var/log/xray/updater.log
    exit 1
fi

echo "[INFO] Update process completed successfully" >> /var/log/xray/updater.log