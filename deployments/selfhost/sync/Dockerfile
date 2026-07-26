FROM alpine:3.20
RUN apk add --no-cache bash git curl jq docker-cli coreutils util-linux
COPY sync-check.sh /usr/local/bin/sync-check.sh
RUN chmod +x /usr/local/bin/sync-check.sh
VOLUME /data
ENTRYPOINT ["/usr/local/bin/sync-check.sh"]
