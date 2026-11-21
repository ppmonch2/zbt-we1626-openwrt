#!/opt/bin/sh
# Opt-in VK heater command pilot. Disabled unless control_enabled=true in private config.
# Messages are HMAC-authenticated, expire after 90 seconds, and are claimed durably
# BEFORE touching the heater. Replayed IDs NEVER re-execute.
set -eu
umask 077
export PATH=/opt/bin:/opt/sbin:/usr/bin:/bin:/usr/sbin:/sbin
cfg="${HB_VK_CONFIG:-/opt/etc/heatbridge/vk-pilot.json}"
work="${HB_VK_CONTROL_RUNTIME:-/tmp/heatbridge-vk-control}"
root="${HB_VK_STATE:-/opt/var/lib/heatbridge-vk-pilot}"
mode="${1:---once}"
case "$mode" in --once|--loop) ;; *) echo INVALID_MODE; exit 2;; esac
for tool in curl jq openssl base64 hexdump; do
 command -v "$tool" >/dev/null 2>&1 || { echo "MISSING_$tool"; exit 1; }
done
[ "$(id -u)" = 0 ] && [ -r "$cfg" ] || { echo INVALID_ACCESS; exit 1; }
# Fail closed without an explicit opt-in; read-only pilot remains unaffected.
if ! jq -e '.control_enabled==true' "$cfg" >/dev/null 2>&1; then
 echo VK_HEATER_CONTROL_DISABLED
 exit 0
fi
token="$(jq -er '.token|select(type=="string")' "$cfg")"
peer="$(jq -er '.peer_id|select(type=="number" and .>0)' "$cfg")"
group="$(jq -er '.group_id|select(type=="number" and .>0)' "$cfg")"
key="$(jq -er '.hmac_key|select(type=="string")' "$cfg")"
case "$token" in ''|*[!A-Za-z0-9._-]*) echo INVALID_TOKEN; exit 1;; esac
case "$key" in ''|*[!a-fA-F0-9]*) echo INVALID_KEY; exit 1;; esac
[ "${#token}" -ge 20 ] && [ "${#token}" -le 512 ] && [ "${#key}" -eq 64 ] &&
[ "$peer" != "$group" ] || { echo INVALID_CONFIG; exit 1; }
[ -x /opt/bin/heatbridge-device ] && [ -r /opt/etc/heatbridge/config.json ] ||
 { echo LOCAL_HEATER_NOT_CONFIGURED; exit 1; }
mkdir -p "$work" "$root/action-claims"
chmod 700 "$work" "$root" "$root/action-claims"
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
valid_hex24() {
 [ "${#1}" -eq 24 ] || return 1
 case "$1" in ''|*[!a-f0-9]*) return 1;; esac
}
valid_hex64() {
 [ "${#1}" -eq 64 ] || return 1
 case "$1" in ''|*[!a-f0-9]*) return 1;; esac
}
send_ack() {
 record="$1"
 [ -s "$record/ack.txt" ] || return 1
 [ ! -f "$record/ack-sent" ] || return 0
 message="$(cat "$record/ack.txt")" || return 1
 random_hex="$(head -c 4 /dev/urandom | hexdump -v -e '1/1 "%02x"')"
 random_id="$((0x$random_hex%2147483646+1))"
 if vk_call messages.send --data-urlencode "peer_id=$peer" \
   --data-urlencode "random_id=$random_id" \
   --data-urlencode "message=$message" > "$work/response.json" &&
   jq -e '.response|type=="number" and .>0' "$work/response.json" >/dev/null 2>&1; then
   : > "$record/ack-sent"
   echo VK_ACTION_ACK_SENT
 else
   echo VK_ACTION_ACK_RETRY_LATER
 fi
}
write_ack() {
 # Called once after the durable nonce claim; no live device data is invented.
 record="$1"
 stamp="$(date +%s)"
 raw="$(jq -c . "$record/ack.json")" || return 1
 encoded="$(printf '%s' "$raw" | base64 | tr -d '\r\n=' | tr '+/' '-_')"
 [ "${#encoded}" -lt 2000 ] || return 1
 body="HB3.ACK.$nonce.$stamp.$encoded"
 signature="$(hmac "$body")" || return 1
 printf '%s.%s\n' "$body" "$signature" > "$record/ack.txt"
}
run_command() {
 nonce="$1"; heater="$2"; action="$3"; value="$4"
 record="$root/action-claims/$nonce"
 if ! mkdir "$record" 2>/dev/null; then
   # Prior delivery, including after process crash, never executes twice.
   [ ! -s "$record/ack.txt" ] || send_ack "$record"
   return 0
 fi
 # Claim is persistent before local heater I/O. An incomplete claim is never
 # re-executed automatically: user must inspect actual temperature separately.
 if ! jq -e --arg heater "$heater" '.heaters[$heater].verified==true' \
    /opt/etc/heatbridge/config.json >/dev/null 2>&1; then
   jq -nc --arg heater "$heater" --arg action "$action" --argjson value "$value" \
      '{heater:$heater,action:$action,value:$value,status:"REJECTED",confirmed:false}' \
      > "$record/ack.json"
 else
   if /opt/bin/heatbridge-device "$action" "$heater" "$value" \
       > "$record/device.json" 2>/dev/null; then
     jq -ce --arg heater "$heater" --arg action "$action" --argjson expected "$value" '
        if (.encrypted_session==true and .command_acknowledged==true
            and .state_echo_matches_request==true
            and (if $action=="set" then .reported_target==$expected
                 else .device_mode==$expected end))
        then {heater:$heater,action:$action,value:$expected,
              status:"DONE",confirmed:true,
              reported_target:(.reported_target // null),
              current_temperature:(.current_temperature // null),
              device_mode:(.device_mode // null)}
        else {heater:$heater,action:$action,value:$expected,
              status:"UNCONFIRMED",confirmed:false} end
     ' "$record/device.json" > "$record/ack.json" || true
   fi
   if [ ! -s "$record/ack.json" ]; then
     jq -nc --arg heater "$heater" --arg action "$action" --argjson value "$value" \
       '{heater:$heater,action:$action,value:$value,status:"UNCONFIRMED",confirmed:false}' \
       > "$record/ack.json"
   fi
 fi
 write_ack "$record" || return 1
 send_ack "$record"
}
process() {
 message="$1"
 [ "${#message}" -le 190 ] || return 0
 original_ifs="$IFS"
 IFS='.'
 read -r version kind nonce stamp heater action value signature trailing <<EOF
$message
EOF
 IFS="$original_ifs"
 [ "$version" = HB3 ] && [ "$kind" = CMD ] && [ -z "$trailing" ] || return 0
 valid_hex24 "$nonce" && valid_hex64 "$signature" || return 0
 case "$stamp" in ''|*[!0-9]*) return 0;; esac
 [ "${#stamp}" -eq 10 ] || return 0
 case "$heater" in h1|h2|h3|h4) ;; *) return 0;; esac
 case "$value" in ''|*[!0-9]*) return 0;; esac
 case "$action" in
   set) [ "$value" -ge 10 ] && [ "$value" -le 35 ] || return 0;;
   mode) [ "$value" = 0 ] || [ "$value" = 1 ] || return 0;;
   *) return 0;;
 esac
 now="$(date +%s)"
 age="$((now-stamp))"
 [ "$age" -ge 0 ] && [ "$age" -le 90 ] || return 0
 body="HB3.CMD.$nonce.$stamp.$heater.$action.$value"
 [ "$(hmac "$body")" = "$signature" ] || return 0
 run_command "$nonce" "$heater" "$action" "$value"
}
poll() {
 if ! vk_call messages.getHistory --data-urlencode "peer_id=$peer" \
   --data-urlencode 'count=100' > "$work/history.json"; then
   echo VK_ACTION_CONNECTION_FAILED; return 1
 fi
 if ! jq -e '.response.items|type=="array"' "$work/history.json" >/dev/null 2>&1; then
   error="$(jq -r '.error.error_code // "INVALID_RESPONSE"' "$work/history.json")"
   echo "VK_ACTION_API_ERROR_$error"; return 1
 fi
 jq -r --argjson peer "$peer" --argjson group "$group" '
   .response.items | reverse[] |
   select(.out==1 and .peer_id==$peer and .from_id==(-$group)) |
   .text | select(type=="string" and startswith("HB3.CMD."))
 ' "$work/history.json" > "$work/commands.txt"
 count=0
 while IFS= read -r line; do
   process "$line"
   count="$((count+1))"
 done < "$work/commands.txt"
 [ "$count" -ne 0 ] || echo VK_ACTION_NO_NEW_COMMANDS
}
case "$mode" in
 --once) poll ;;
 --loop)
  echo VK_ACTION_AGENT_RUNNING
  while :; do
   poll || true
   sleep 4
  done ;;
esac
