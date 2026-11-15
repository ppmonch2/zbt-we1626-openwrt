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
[ ! -d /tmp/heatbridge/device.lock ] || { echo 'A local operation is still running; wait for it to finish before installing.' >&2; exit 1; }
if [ -x /opt/etc/init.d/S85heatbridge-alice ]; then
 /opt/etc/init.d/S85heatbridge-alice stop
fi
mkdir -p /opt/bin /opt/etc/init.d /opt/var/lib/heatbridge-alice/requests
chmod 700 /opt/etc/heatbridge /opt/var/lib/heatbridge-alice /opt/var/lib/heatbridge-alice/requests
chmod 600 /opt/etc/heatbridge/alice.json
# Retain the old wrapper for rollback; native client and credentials are unchanged.
[ -f /opt/bin/heatbridge-device.before-alice-state ] || \
 cp /opt/bin/heatbridge-device /opt/bin/heatbridge-device.before-alice-state
for name in heatbridge-alice heatbridge-alice-worker heatbridge-alice-state heatbridge-device; do
 cp "$here/$name" "/opt/bin/$name.new"
 chmod 755 "/opt/bin/$name.new"
 mv "/opt/bin/$name.new" "/opt/bin/$name"
done
cp "$here/S85heatbridge-alice" /opt/etc/init.d/
chmod 755 /opt/bin/heatbridge-alice /opt/bin/heatbridge-alice-worker /opt/etc/init.d/S85heatbridge-alice
echo 'Files installed. Agent was NOT started.'
echo 'After checking cloud endpoints and private settings: /opt/etc/init.d/S85heatbridge-alice start'
