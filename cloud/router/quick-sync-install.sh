#!/opt/bin/sh
# Only fast telemetry publication after a verified Disk command; no firmware.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
[ "$(id -u)" = 0 ] || { echo 'Entware root required'; exit 1; }
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
[ -x /opt/etc/init.d/S85heatbridge-alice ] || { echo 'Alice daemon missing'; exit 1; }
[ -s /opt/etc/heatbridge/disk.json ] || { echo 'Disk config missing'; exit 1; }
[ -x /opt/bin/heatbridge-alice-state ] && [ -x /opt/bin/heatbridge-disk-agent ] || {
 echo 'Existing router scripts missing'; exit 1;
}
(cd "$here" && sha256sum -c SHA256SUMS) || exit 1
stage="$(mktemp -d /opt/heatbridge-priority.XXXXXX)" || exit 1
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
for name in heatbridge-alice-state heatbridge-disk-agent; do
 cp -p "/opt/bin/$name" "$stage/$name"
done
init=/opt/etc/init.d/S85heatbridge-alice
running=false
if "$init" status >/dev/null 2>&1; then running=true; fi
if [ "$running" = true ]; then
 "$init" stop >/dev/null 2>&1 || { echo 'Cannot stop agent; no changes made'; exit 1; }
fi
for name in heatbridge-alice-state heatbridge-disk-agent; do
 cp "$here/$name" "/opt/bin/$name.new"
 chmod 755 "/opt/bin/$name.new"
 mv "/opt/bin/$name.new" "/opt/bin/$name"
done
if [ "$running" = true ]; then
 if ! "$init" start >/dev/null 2>&1 || ! "$init" status >/dev/null 2>&1; then
  "$init" stop >/dev/null 2>&1 || true
  for name in heatbridge-alice-state heatbridge-disk-agent; do
   cp "$stage/$name" "/opt/bin/$name.new"
   chmod 755 "/opt/bin/$name.new"
   mv "/opt/bin/$name.new" "/opt/bin/$name"
  done
  "$init" start >/dev/null 2>&1 || true
  echo 'New agent did not start. Previous scripts restored.'
  exit 1
 fi
 echo 'OK: priority publication installed and running.'
else
 echo 'OK: scripts installed. The Alice daemon was already stopped; not started.'
fi
