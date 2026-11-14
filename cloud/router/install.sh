#!/opt/bin/sh
# An optional add-on, separate from the already working local app.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
[ "$(id -u)" = 0 ] || { echo 'Run as Entware root.' >&2; exit 1; }
[ -x /opt/bin/heatbridge-device ] && [ -s /opt/etc/heatbridge/config.json ] || { echo 'Install and verify the local app first.' >&2; exit 1; }
[ -s /opt/etc/heatbridge/alice.json ] || { echo 'Create the private cloud configuration first.' >&2; exit 1; }
curl -q --help all | grep -q -- '--aws-sigv4' || { echo 'This curl does not support AWS SigV4.' >&2; exit 1; }
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
(cd "$here" && sha256sum -c SHA256SUMS) || exit 1
mkdir -p /opt/bin /opt/etc/init.d /opt/var/lib/heatbridge-alice/requests
chmod 700 /opt/etc/heatbridge /opt/var/lib/heatbridge-alice /opt/var/lib/heatbridge-alice/requests
chmod 600 /opt/etc/heatbridge/alice.json
cp "$here/heatbridge-alice" "$here/heatbridge-alice-worker" /opt/bin/
cp "$here/S85heatbridge-alice" /opt/etc/init.d/
chmod 755 /opt/bin/heatbridge-alice /opt/bin/heatbridge-alice-worker /opt/etc/init.d/S85heatbridge-alice
echo 'Files installed. Agent was NOT started.'
echo 'After checking cloud endpoints and private settings: /opt/etc/init.d/S85heatbridge-alice start'
