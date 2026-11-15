FROM debian:12-slim

# Základní build deps + dev knihovny, které Asterisk potřebuje
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates pkg-config subversion\
    libssl-dev libxml2-dev libsqlite3-dev uuid-dev \
    libjansson-dev libedit-dev libcurl4-openssl-dev \
    libsrtp2-dev libncurses5-dev \
    net-tools iproute2 iputils-ping \
 && rm -rf /var/lib/apt/lists/*

# Stáhnout a postavit Asterisk 20 s bundled PJPROJECT a SRTP
ENV ASTSRC=/usr/src/asterisk
RUN mkdir -p ${ASTSRC} \
 && curl -L https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-20-current.tar.gz \
    -o /tmp/asterisk.tar.gz \
 && tar -xzf /tmp/asterisk.tar.gz -C ${ASTSRC} --strip-components=1 \
 && rm /tmp/asterisk.tar.gz \
 && cd ${ASTSRC} \
 && contrib/scripts/get_mp3_source.sh \
 && ./configure --with-pjproject-bundled --with-ssl=ssl --with-srtp \
 && make menuselect.makeopts \
 && menuselect/menuselect \
      --enable res_srtp \
      --enable chan_pjsip \
      --enable res_pjsip \
      --enable CORE-SOUNDS-EN-WAV \
      --enable MOH-OPSOUND-WAV \
      menuselect.makeopts \
 && make -j"$(nproc)" \
 && make install \
 && make samples \
 && ldconfig

# (volitelné) vytvořit uživatele asterisk a vlastnictví cest
RUN useradd -r -m -d /var/lib/asterisk -s /sbin/nologin asterisk \
 && chown -R asterisk:asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk || true

# Runtime
# Síť doporučuju v docker-compose nastavit `network_mode: host`
# aby 5060/5061/10000-20000 nebylo třeba mapovat.
CMD ["asterisk","-f","-U","root","-vvv"]
