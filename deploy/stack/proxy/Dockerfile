# The one Caddyfile, baked in: routes version with the release, and an upgrade
# is a new image tag, which compose always recreates — unlike a bind-mounted
# file edited in place (the inode class this replaces).
FROM caddy:2.11-alpine
COPY Caddyfile /etc/caddy/Caddyfile
RUN mkdir -p /etc/caddy/extra
LABEL org.opencontainers.image.title="pystino-proxy" \
      org.opencontainers.image.source="https://github.com/paoloviviani/Pystino" \
      org.opencontainers.image.licenses="EUPL-1.2"
