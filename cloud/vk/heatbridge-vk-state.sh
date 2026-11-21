#!/opt/bin/sh
# VK stage 2: a signed, read-only snapshot of cached heater state.
# Never calls heatbridge-device, sends actuator commands or modifies the old bridge.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
cfg="${HB_VK_CONFIG:-/opt/etc/heatbridge/vk-pilot.json}"
work="${HB_VK_RUNTIME:-/tmp/heatbridge-vk-state}"
root="${HB_VK_STATE:-/opt/var/lib/heatbridge-vk-pilot}"
cache="${HEATBRIDGE_DEVICE_RUNTIME:-/tmp/heatbridge}/states"
for tool in curl jq openssl base64 hexdump; do
 command -v "$tool" >/dev/null 2>&1 || { echo "MISSING_$tool"; exit 1; }
done
[ "$(id -u)" = 0 ] && [ -r "$cfg" ] || { echo INVALID_ACCESS; exit 1; }
token="$(jq -er '.token|select(type=="string")' "$cfg")"
peer="$(jq -er '.peer_id|select(type=="number" and .>0)' "$cfg")"
group="$(jq -er '.group_id|select(type=="number" and .>0)' "$cfg")"
key="$(jq -er '.hmac_key|select(type=="string")' "$cfg")"
case "$token" in ''|*[!A-Za-z0-9._-]*) echo INVALID_TOKEN; exit 1;; esac
case "$key" in ''|*[!a-fA-F0-9]*) echo INVALID_KEY; exit 1;; esac
[ "${#token}" -ge 20 ] && [ "${#token}" -le 512 ] && [ "${#key}" -eq 64 ] ||
  { echo INVALID_CONFIG; exit 1; }
mkdir -p "$work" "$root/seen-read"
chmod 700 "$work" "$root" "$root/seen-read"
auth="$work/curl.conf"
jq -er '"data-urlencode = " + (("access_token=" + .token)|@json)' "$cfg" > "$auth.new"
chmod 600 "$auth.new"; mv "$auth.new" "$auth"
vk_call() {
 method="$1"; shift
 case "$method" in messages.send|messages.getHistory) ;; *) return 1;; esac
 curl -q --config "$auth" --noproxy '*' --proto '=https' \
  --connect-timeout 4 --max-time 9 --max-filesize 131072 -fsS \
  --data-urlencode 'v=5.199' "$@" "https://api.vk.com/method/$method"
}
hmac() {
 printf '%s' "$1" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$key" 2>/dev/null |
  sed 's/^.*= //'
}
hex24() {
 [ "${#1}" = 24 ] || return 1
 case "$1" in *[!a-f0-9]*) return 1;; esac
}
hex64() {
 [ "${#1}" = 64 ] || return 1
 case "$1" in *[!a-f0-9]*) return 1;; esac
}
send_snapshot() {
 nonce="$1"
 now="$(date +%s)"
 # Cached state is read-only. Do not contact a heater or alter its setpoint.
 set --
 for id in h1 h2 h3 h4; do
  [ ! -s "$cache/$id.json" ] || set -- "$@" "$cache/$id.json"
 done
 jq -cs --slurpfile config /opt/etc/heatbridge/config.json '
   {schema:1,devices:
    (map(select(.online==true and .result.encrypted_session==true and
       (.heater as $id | $config[0].heaters[$id].verified==true) and
       (.heater=="h1" or .heater=="h2" or .heater=="h3" or .heater=="h4") and
       (.observed_at|type=="number") and
       (.result.reported_target|type=="number") and
       (.result.current_temperature|type=="number") and
       (.result.device_mode|type=="number"))) |
     map({key:.heater,value:{
       observed_at,
       reported_target:.result.reported_target,
       current_temperature:.result.current_temperature,
       device_mode:.result.device_mode}}) | from_entries)}
 ' "$@" < /dev/null > "$work/snapshot.json" || return 1
 [ "$(wc -c < "$work/snapshot.json")" -le 1300 ] || return 1
 encoded="$(base64 < "$work/snapshot.json" | tr -d '\r\n=' | tr '+/' '-_')"
 body="HB2.STATE.$nonce.$now.$encoded"
 signature="$(hmac "$body")" || return 1
 message="$body.$signature"
 # Fresh random_id, no dependence on BusyBox od option support.
 random_hex="$(head -c 4 /dev/urandom | hexdump -v -e '1/1 "%02x"')"
 random_id="$((0x$random_hex%2147483646+1))"
 if vk_call messages.send --data-urlencode "peer_id=$peer" \
    --data-urlencode "random_id=$random_id" \
    --data-urlencode "message=$message" > "$work/sent.json" &&
    jq -e '.response|type=="number" and .>0' "$work/sent.json" >/dev/null 2>&1; then
  echo VK_STATE_SENT
 else
  echo VK_STATE_SEND_FAILED
  return 1
 fi
}
process_read() {
 message="$1"
 [ "${#message}" -le 155 ] || return 0
 previous_ifs="$IFS"
 IFS='.'
 read -r version kind nonce stamp signature trailing <<EOF
$message
EOF
 IFS="$previous_ifs"
 [ "$version" = HB2 ] && [ "$kind" = READ ] && [ -z "$trailing" ] ||
   return 0
 hex24 "$nonce" && hex64 "$signature" || return 0
 case "$stamp" in ''|*[!0-9]*) return 0;; esac
 [ "${#stamp}" = 10 ] || return 0
 now="$(date +%s)"
 age="$((now-stamp))"
 [ "$age" -ge 0 ] && [ "$age" -le 120 ] || return 0
 body="HB2.READ.$nonce.$stamp"
 [ "$(hmac "$body")" = "$signature" ] || return 0
 if ! mkdir "$root/seen-read/$nonce" 2>/dev/null; then return 0; fi
 if ! send_snapshot "$nonce"; then
  rmdir "$root/seen-read/$nonce" 2>/dev/null || true
  return 1
 fi
}
if ! vk_call messages.getHistory --data-urlencode "peer_id=$peer" \
   --data-urlencode 'count=100' > "$work/history.json"; then
 echo VK_HISTORY_CONNECTION_FAILED; exit 1
fi
if ! jq -e '.response.items|type=="array"' "$work/history.json" >/dev/null 2>&1; then
 error="$(jq -r '.error.error_code // "INVALID_RESPONSE"' "$work/history.json")"
 echo "VK_HISTORY_API_ERROR_$error"
 exit 1
fi
jq -r --argjson peer "$peer" --argjson group "$group" '
 .response.items[] | select(.out==1 and .peer_id==$peer and .from_id==(-$group)) |
 .text | select(type=="string" and startswith("HB2.READ."))
 ' "$work/history.json" > "$work/requests.txt"
count=0
while IFS= read -r line; do
 process_read "$line"
 count="$((count+1))"
done < "$work/requests.txt"
[ "$count" -ne 0 ] || echo VK_API_ACCESS_OK_NO_STATE_REQUESTS
