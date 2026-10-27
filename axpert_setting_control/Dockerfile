FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.18" \
  io.hass.type="addon" \
  io.hass.arch="aarch64"

ENV LANG=C.UTF-8

RUN apk add --no-cache \
    jq \
    python3 \
    py3-crcmod \
    py3-paho-mqtt

COPY run.sh monitor.py test_qpigs2.py test_setting_control.py /

RUN chmod a+x /run.sh /monitor.py

CMD ["/run.sh"]
