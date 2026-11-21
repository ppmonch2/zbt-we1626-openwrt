#!/opt/bin/sh
# VK mailbox proof-of-concept. ONLY answers signed PING; NEVER touches heaters.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
config="${HB_VK_CONFIG:-/opt/etc/heatbridge/vk-pilot.json}"
runtime="${HB_VK_RUNTIME:-/tmp/heatbridge-vk-pilot}"
state="${HB_VK_STATE:-/opt/var/lib/heatbridge-vk-pilot}"
case "${1:---once}" in --once|--loop) mode="$1";; *) echo 'Usage: heatbridge-vk-pilot.sh [--once|--loop]' >&2; exit 2;; esac
for program in curl jq openssl; do
 command -v "$program" >/dev/null 2>&1 || { echo "MISSING_DEPENDENCY: $program"; exit 1; }
done
[ -r "$config" ] || { echo 'MISSING_VK_CONFIG'; exit 1; }
# Config is private, contains the group token and a 32-byte HMAC secret.
[ "$(id -u)" = 0 ] || { echo 'RUN_AS_ENTWARE_ROOT'; exit 1; }
token="$(jq -er '.token | select(type=="string")' "$config")" || exit 1
peer="$(jq -er '.peer_id | select(type=="number" and .>0)' "$config")" || exit 1
group="$(jq -er '.group_id | select(type=="number" and .>0)' "$config")" || exit 1
key="$(jq -er '.hmac_key | select(type=="string")' "$config")" || exit 1
case "$token" in *[!A-Za-z0-9._-]*|'') echo INVALID_TOKEN; exit 1;; esac
case "$peer:$group" in *[!0-9:]*|'') echo INVALID_ID; exit 1;; esac
case "$key" in *[!a-fA-F0-9]*|'') echo INVALID_HMAC_KEY; exit 1;; esac
[ "${#token}" -ge 20 ] && [ "${#token}" -le 512 ] &&
[ "${#key}" -eq 64 ] && [ "$peer" != "$group" ] || { echo INVALID_CONFIG; exit 1; }
mkdir -p "$runtime" "$state/seen"
chmod 700 "$runtime" "$state" "$state/seen"
auth="$runtime/vk-curl.conf"
jq -er '"data-urlencode = " + (("access_token=" + .token)|@json)' "$config" > "$auth.new"
chmod 600 "$auth.new"; mv "$auth.new" "$auth"
vk_call() {
 method="$1"; shift
 case "$method" in messages.send|messages.getHistory) ;; *) return 1;; esac
 curl -q --config "$auth" --noproxy '*' --proto '=https' \
  --connect-timeout 4 --max-time 9 --max-filesize 131072 -fsS \
  --data-urlencode 'v=5.199' "$@" \
  "https://api.vk.com/method/$method"
}
hmac() {
 printf '%s' "$1" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$key" 2>/dev/null |
  sed 's/^.*= //'
}
is_hex() {
 case "$1" in ''|*[!a-f0-9]*) return 1;; *) return 0;; esac
}
process() {
 message="$1"
 case "$message" in HB1.PING.*) ;; *) return 0;; esac
 [ "${#message}" -le 160 ] || return 0
 previous_ifs="$IFS"
 IFS='.'
 read -r version kind nonce stamp signature <<EOF
$message
EOF
 IFS="$previous_ifs"
 [ "$version" = HB1 ] && [ "$kind" = PING ] || return 0
 is_hex "$nonce" && [ "${#nonce}" = 24 ] || return 0
 is_hex "$signature" && [ "${#signature}" = 64 ] || return 0
 case "$stamp" in ''|*[!0-9]*) return 0;; esac
 [ "${#stamp}" = 10 ] || return 0
 now="$(date +%s)"
 age="$((now-stamp))"
 [ "$age" -ge 0 ] && [ "$age" -le 120 ] || return 0
 body="HB1.PING.$nonce.$stamp"
 verified="$(hmac "$body")" || return 0
 [ "$verified" = "$signature" ] || return 0
 if ! mkdir "$state/seen/$nonce" 2>/dev/null; then return 0; fi
 answer_body="HB1.PONG.$nonce.$now"
 answer_mac="$(hmac "$answer_body")" || return 1
 answer="$answer_body.$answer_mac"
 random_id="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"
 random_id="$((random_id%2147483646+1))"
 if vk_call messages.send \
   --data-urlencode "peer_id=$peer" \
   --data-urlencode "random_id=$random_id" \
   --data-urlencode "message=$answer" > "$runtime/sent.json" &&
   jq -e '.response | type=="number" and .>0' "$runtime/sent.json" >/dev/null 2>&1; then
  echo 'VK_PONG_SENT'
 else
  rmdir "$state/seen/$nonce" 2>/dev/null || true
  echo 'VK_PONG_SEND_FAILED'
 fi
}
poll() {
 if ! vk_call messages.getHistory --data-urlencode "peer_id=$peer" \
     --data-urlencode 'count=100' > "$runtime/history.json"; then
  echo 'VK_HISTORY_CONNECTION_FAILED'; return 1
 fi
 if ! jq -e '.response.items | type=="array"' "$runtime/history.json" >/dev/null 2>&1; then
  error="$(jq -r '.error.error_code // "INVALID_RESPONSE"' "$runtime/history.json")"
  echo "VK_HISTORY_API_ERROR_$error"
  return 1
 fi
 jq -r --argjson peer "$peer" --argjson group "$group" '
   .response.items[] |
   select(.out==1 and .peer_id==$peer and .from_id==(-$group)) |
   .text | select(type=="string" and startswith("HB1.PING."))
 ' "$runtime/history.json" > "$runtime/pings.txt"
 count=0
 while IFS= read -r entry; do
  process "$entry"
  count="$((count+1))"
 done < "$runtime/pings.txt"
 [ "$count" -ne 0 ] || echo VK_API_ACCESS_OK_NO_NEW_PINGS
}
case "$mode" in
 --once) poll ;;
 --loop)
  echo 'VK_PILOT_RUNNING_NO_HEATER_CONTROL'
  while :; do
   poll || true
   sleep 7
  done ;;
esac
