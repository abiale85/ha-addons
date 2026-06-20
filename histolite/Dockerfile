ARG BUILD_FROM
FROM $BUILD_FROM

# Installa dipendenze di sistema
RUN apk add --no-cache \
    sqlite \
    tzdata

# Copia requirements e installa dipendenze Python
COPY rootfs/opt/histolite/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copia l'intera struttura rootfs
COPY rootfs /

# Rendi eseguibile lo script di avvio
RUN chmod +x /run.sh

CMD ["/run.sh"]
