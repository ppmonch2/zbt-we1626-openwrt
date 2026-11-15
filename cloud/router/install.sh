#!/opt/bin/sh
# An optional add-on, separate from the already working local app.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
[ "$(id -u)" = 0 ] || { echo 'Run as Entware root.' >&2; exit 1; }
[ -x /opt/bin/heatbridge-device ] && [ -s /opt/etc/heatbridge/config.json ] || { echo 'Install and verify the local app first.' >&2; exit 1; }
command -v curl >/dev/null && command -v jq >/dev/null || { echo 'curl and jq are required.' >&2; exit 1; }
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
(cd "$here" && sha256sum -c SHA256SUMS) || exit 1
[ ! -d /tmp/heatbridge/device.lock ] || { echo 'A local operation is still running; wait for it to finish before installing.' >&2; exit 1; }
if [ -x /opt/etc/init.d/S85heatbridge-alice ]; then
 /opt/etc/init.d/S85heatbridge-alice stop
fi
mkdir -p /opt/bin /opt/etc/init.d /opt/var/lib/heatbridge-alice/requests
chmod 700 /opt/etc/heatbridge /opt/var/lib/heatbridge-alice /opt/var/lib/heatbridge-alice/requests
[ ! -f /opt/etc/heatbridge/alice.json ] || chmod 600 /opt/etc/heatbridge/alice.json
# Retain the old wrapper for rollback; native client and credentials are unchanged.
[ -f /opt/bin/heatbridge-device.before-alice-state ] || \
 cp /opt/bin/heatbridge-device /opt/bin/heatbridge-device.before-alice-state
for name in heatbridge-alice heatbridge-alice-worker heatbridge-alice-state heatbridge-device \
 heatbridge-disk-lib heatbridge-disk-agent heatbridge-disk-batch heatbridge-disk-control; do
 cp "$here/$name" "/opt/bin/$name.new"
 chmod 755 "/opt/bin/$name.new"
 mv "/opt/bin/$name.new" "/opt/bin/$name"
done
cp "$here/S85heatbridge-alice" /opt/etc/init.d/
chmod 755 /opt/bin/heatbridge-alice /opt/bin/heatbridge-alice-worker /opt/etc/init.d/S85heatbridge-alice
web=/opt/share/heatbridge/www
mkdir -p "$web/cgi-bin"
if [ -f "$web/index.html" ] && [ ! -f /opt/etc/heatbridge/index.before-disk.html ]; then
 cp "$web/index.html" /opt/etc/heatbridge/index.before-disk.html
fi
for name in index.html app.js disk.html disk.js cgi-bin/api cgi-bin/disk-api; do
 cp "$here/www/$name" "$web/$name.new"
 case "$name" in cgi-bin/*) chmod 755 "$web/$name.new";; *) chmod 644 "$web/$name.new";; esac
 mv "$web/$name.new" "$web/$name"
done
echo 'Files installed. Agent was NOT started.'
echo 'Disk setup: open http://192.168.1.1:8765/disk.html on your local Wi-Fi.'
echo 'After checking cloud endpoints and private settings: /opt/etc/init.d/S85heatbridge-alice start'
