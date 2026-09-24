# The bundled IdP: upstream Authelia plus one static configuration file.
#
# The file holds no secret and no key. Authelia's template filter
# (X_AUTHELIA_CONFIG_FILTERS=template, set in compose.yaml) fills it from the
# container's environment and from /config/keys/jwks.pem, which `pystino
# bootstrap` creates once and never overwrites. So:
#   - a new OIDC client ships as a new image tag → compose recreates → live;
#   - a rotated secret changes the environment → compose recreates → live;
#   - the signing key and the users file are state, and nothing regenerates them.
# That split is what the old generator could not offer: it wrote clients, keys
# and users into one file, so it refused to run twice.
FROM authelia/authelia:4.39.22
COPY configuration.yml /etc/authelia/configuration.yml
CMD ["--config", "/etc/authelia/configuration.yml"]
LABEL org.opencontainers.image.title="pystino-authelia" \
      org.opencontainers.image.source="https://github.com/paoloviviani/Pystino" \
      org.opencontainers.image.licenses="Apache-2.0"
