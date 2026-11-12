# Japan Quake Monitor - static dashboard served by nginx (unprivileged, port 8080).
# The upstream feed connections are made by the visitor's browser, so the
# container itself needs no outbound configuration.
FROM nginxinc/nginx-unprivileged:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
# the alert chime lives here once it is no longer inlined in index.html
COPY assets/ /usr/share/nginx/html/assets/

EXPOSE 8080
