#!/bin/sh
# Scheduled/manual outbound call: Asterisk dials the senior via Odorik; on answer the [scheduled]
# dialplan bridges to OpenAI with X-Mode: scheduled. Usage: call-now.sh 00420123456789
# cron example: 0 10 * * * /path/to/senior-voice/deploy/call-now.sh 00420123456789
set -e
N=${1:?number, e.g. 00420123456789}
docker exec asterisk asterisk -rx "channel originate PJSIP/$N@od-trunk-endpoint extension s@scheduled"
