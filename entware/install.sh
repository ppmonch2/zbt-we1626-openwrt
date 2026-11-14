#!/opt/bin/sh
# Installs only to Entware /opt. Does not write firmware, Wi-Fi, DNS or HOMMYN.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
lan=192.168.1.1
if [ "$#" -gt 0 ]; then
 [ "$#" = 2 ] && [ "$1" = --lan-ip ] || { echo 'Usage: install.sh [--lan-ip 192.168.1.1]' >&2;exit 1; }
 lan="$2"
fi
printf '%s\n' "$lan" | awk -F. 'NF!=4{exit 1}{for(i=1;i<=4;i++)if($i!~/^[0-9]+$/ || $i>255)exit 1;if(!($1==10 || ($1==192&&$2==168) || ($1==172&&$2>=16&&$2<=31)))exit 1}' || { echo 'A private LAN IPv4 address is required.' >&2;exit 1; }
[ "$(id -u)" = 0 ] || { echo 'Run from the Entware root shell.' >&2;exit 1; }
[ -x /opt/bin/opkg ] || { echo 'Install Entware on the EXT drive first.' >&2;exit 1; }
case "$(uname -m)" in mips*) ;; *) echo 'This bundle is for MIPS little-endian MT7620.' >&2;exit 1;; esac
uname -r | awk -F. '{if($1<3 || ($1==3&&$2<4))exit 1}' || { echo 'Entware mipselsf-k3.4 requires kernel 3.4 or later.' >&2;exit 1; }
(cd "$here" && sha256sum -c SHA256SUMS) || { echo 'Bundle integrity check failed.' >&2;exit 1; }
free_kb="$(df -Pk /opt | awk 'NR==2{print $4}')"
[ "${free_kb:-0}" -ge 65536 ] || { echo 'At least 64 MiB free space on the USB drive is required.' >&2;exit 1; }
/opt/bin/opkg update
new_httpd=1
/opt/bin/opkg status uhttpd_kn 2>/dev/null | grep -q 'Status:.* installed' && new_httpd=0
/opt/bin/opkg install uhttpd_kn jq openssl-util
# A newly installed generic server is not needed: this application has its own LAN listener.
if [ "$new_httpd" = 1 ]; then
 for init in /opt/etc/init.d/S*uhttpd*; do
  [ ! -f "$init" ] || { "$init" stop 2>/dev/null || true; chmod -x "$init"; }
 done
fi
mkdir -p /opt/etc/heatbridge /opt/bin /opt/libexec /opt/etc/init.d /opt/share/heatbridge
chmod 700 /opt/etc/heatbridge
if [ ! -f /opt/etc/heatbridge/config.json ]; then
 jq -n --arg lan "$lan" '{lan_ip:$lan,heaters:{
  h1:{name:"Холл_Окно",temp:18,verified:false},
  h2:{name:"Мансарда",temp:18,verified:false},
  h3:{name:"Холл_Лестница",temp:18,verified:false},
  h4:{name:"Кухня",temp:18,verified:false}}}' > /opt/etc/heatbridge/config.json
fi
chmod 600 /opt/etc/heatbridge/config.json
cp "$here/bin/heatbridge-device" /opt/bin/
cp "$here/bin/heatbridge-syncleo" /opt/libexec/
cp "$here/init.d/S80heatbridge" /opt/etc/init.d/
cp -R "$here/www" /opt/share/heatbridge/
chmod 755 /opt/bin/heatbridge-device /opt/libexec/heatbridge-syncleo /opt/etc/init.d/S80heatbridge /opt/share/heatbridge/www/cgi-bin/api
if [ ! -s /opt/etc/heatbridge/httpd.conf ]; then
 echo 'Choose a NEW password for the local web user heatbridge (12+ characters).'
 # A terminal is mandatory so credentials cannot accidentally come from a download pipe.
 [ -r /dev/tty ] || { echo 'Run interactively over SSH.' >&2;exit 1; }
 trap 'stty echo < /dev/tty 2>/dev/null || true' EXIT HUP INT TERM
 while :; do
  printf 'Password: ' > /dev/tty;stty -echo < /dev/tty;IFS= read -r password < /dev/tty
  printf '\nRepeat: ' > /dev/tty;IFS= read -r again < /dev/tty;stty echo < /dev/tty;printf '\n' > /dev/tty
  [ "${#password}" -ge 12 ] && [ "$password" = "$again" ] && break
  echo 'Passwords must match and contain at least 12 characters.'
 done
 hash="$(printf '%s\n' "$password" | openssl passwd -6 -stdin)"
 unset password again
 case "$hash" in '$6$'*) ;; *) echo 'Password hashing failed.' >&2;exit 1;; esac
 printf '/:heatbridge:%s\n' "$hash" > /opt/etc/heatbridge/httpd.conf
 chmod 600 /opt/etc/heatbridge/httpd.conf
 trap - EXIT HUP INT TERM
fi
# Confirm that the native executable can start without a dynamic OpenWrt loader.
if /opt/libexec/heatbridge-syncleo invalid >/dev/null 2>&1; then
 echo 'Unexpected native client response.' >&2;exit 1
else
 code=$?;[ "$code" = 1 ] || { echo "Native client failed to start (exit $code)." >&2;exit 1; }
fi
/opt/etc/init.d/S80heatbridge restart
echo 'Add each heater using its PRIVATE HOMMYN Share link, then press Check state.'
echo 'Keep the existing HOMMYN/Alice link. No router port forwarding is needed.'
