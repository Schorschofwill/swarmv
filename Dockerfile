# syntax=docker/dockerfile:1
#
# Das Dashboard als Container. Zwei Stufen: die erste baut das Rad aus dem
# Quellbaum, die zweite installiert nur dieses Rad. So liegt weder uv noch
# ein Compiler noch der Quellbaum im Laufzeit-Abbild.
#
# Bauen:
#   docker build -t swarmq-dashboard:0.1.0 .
#
# Fahren, hinter einem vorgelagerten TLS-Abschluss (der Normalfall):
#   docker run -d --name sqd -p 8080:8080 \
#     -e SWARMQ_BROKER_URL="amqp://user:pass@rabbit:5672/" \
#     -e SWARMQ_BACKEND_URL="valkey://valkey:6379/0" \
#     -v sqd-data:/var/lib/sqd \
#     swarmq-dashboard:0.1.0
#
# Oder mit eigenem TLS -- dann die Zertifikate hineinreichen und den Befehl
# ersetzen:
#   docker run -d -p 8443:8443 -v /etc/ssl/sqd:/certs:ro ... \
#     swarmq-dashboard:0.1.0 serve --host 0.0.0.0 --port 8443 \
#     --tls-cert /certs/cert.pem --tls-key /certs/key.pem

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS bau
WORKDIR /bau
# Erst die Dateien, die sich selten aendern: so bleibt die Abhaengigkeits-
# schicht ueber gewoehnliche Codeaenderungen hinweg im Cache.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /rad

FROM python:3.14-slim AS laufzeit

# Ein eigener Benutzer, und das Datenverzeichnis gehoert ihm. Der Dienst
# legt seine beiden Dateien mit 0600 an und das Verzeichnis mit 0700 -- als
# root liefe das zwar auch, aber dann gehoerten die Dateien im Volume
# hinterher root, und ein spaeterer Wechsel auf einen Benutzer waere ein
# Rechte-Problem statt einer Einstellung.
RUN useradd --system --create-home --home-dir /home/sqd --shell /usr/sbin/nologin sqd \
 && mkdir -p /var/lib/sqd \
 && chown sqd:sqd /var/lib/sqd \
 && chmod 0700 /var/lib/sqd

COPY --from=bau /rad/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

USER sqd
WORKDIR /home/sqd

# Das Datenverzeichnis ist zustandsbehaftet: die Kontendatei steht in keinem
# Strom und kaeme nach einem Verlust nicht wieder (KTD8). Ohne Volume waere
# sie beim ersten `docker rm` weg -- mitsamt aller Konten.
VOLUME ["/var/lib/sqd"]
ENV SQD_DATA_DIR=/var/lib/sqd
EXPOSE 8080

# Der Dienst haelt keine Datenbank, sondern liest einen Valkey-Strom; ein
# einfacher Aufruf der Anmeldeseite sagt, ob er antwortet. Bewusst nicht
# /api/status: die Seite ist oeffentlich, der Status braucht eine Sitzung.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=4).status == 200 else 1)"

ENTRYPOINT ["swarmq-dashboard"]
# Kein eigenes TLS: im Container ist der vorgelagerte Abschluss der
# Normalfall, und auf 127.0.0.1 zu lauschen erreichte kein Portmapping.
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--behind-tls-proxy"]
