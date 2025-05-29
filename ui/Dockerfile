FROM nginx:alpine

# Copy the HTML file to nginx's default directory
COPY index.html /usr/share/nginx/html/

# Copy a custom nginx configuration if needed (optional)
# COPY nginx.conf /etc/nginx/nginx.conf

# Create a custom entrypoint script for proper signal handling
RUN echo '#!/bin/sh' > /entrypoint.sh && \
    echo 'set -e' >> /entrypoint.sh && \
    echo '' >> /entrypoint.sh && \
    echo '# Function to handle shutdown' >> /entrypoint.sh && \
    echo 'shutdown() {' >> /entrypoint.sh && \
    echo '    echo "Received shutdown signal, stopping nginx..."' >> /entrypoint.sh && \
    echo '    nginx -s quit' >> /entrypoint.sh && \
    echo '    wait $nginx_pid' >> /entrypoint.sh && \
    echo '    exit 0' >> /entrypoint.sh && \
    echo '}' >> /entrypoint.sh && \
    echo '' >> /entrypoint.sh && \
    echo '# Set up signal handlers' >> /entrypoint.sh && \
    echo 'trap shutdown SIGTERM SIGINT' >> /entrypoint.sh && \
    echo '' >> /entrypoint.sh && \
    echo '# Start nginx in background' >> /entrypoint.sh && \
    echo 'nginx -g "daemon off;" &' >> /entrypoint.sh && \
    echo 'nginx_pid=$!' >> /entrypoint.sh && \
    echo '' >> /entrypoint.sh && \
    echo '# Wait for nginx to finish' >> /entrypoint.sh && \
    echo 'wait $nginx_pid' >> /entrypoint.sh && \
    chmod +x /entrypoint.sh

# Expose port 80
EXPOSE 80

# Use custom entrypoint
ENTRYPOINT ["/entrypoint.sh"]
