# Windows PowerShell 5.1+. Read-only IPv4 mDNS discovery, no device commands.
param([string]$LocalIP = '', [int]$Seconds = 15)
$ErrorActionPreference = 'Stop'
if ($Seconds -lt 3 -or $Seconds -gt 60) { throw 'Seconds must be 3..60.' }

function Read-U16([byte[]]$Data, [int]$Pos) {
    if ($Pos -lt 0 -or $Pos + 2 -gt $Data.Length) { throw 'Short DNS packet.' }
    return ([int]$Data[$Pos] * 256 + [int]$Data[$Pos + 1])
}
function Read-DnsName([byte[]]$Data, [ref]$Offset) {
    $pos = [int]$Offset.Value
    $next = -1
    $labels = New-Object 'System.Collections.Generic.List[string]'
    for ($step = 0; $step -lt 128; $step++) {
        if ($pos -lt 0 -or $pos -ge $Data.Length) { throw 'Bad DNS name.' }
        $length = [int]$Data[$pos]
        if (($length -band 192) -eq 192) {
            if ($pos + 1 -ge $Data.Length) { throw 'Bad DNS pointer.' }
            if ($next -lt 0) { $next = $pos + 2 }
            $pos = (($length -band 63) * 256) + [int]$Data[$pos + 1]
            continue
        }
        if ($length -gt 63) { throw 'Bad DNS label.' }
        $pos++
        if ($length -eq 0) {
            if ($next -lt 0) { $next = $pos }
            $Offset.Value = $next
            return ($labels -join '.')
        }
        if ($pos + $length -gt $Data.Length) { throw 'Short DNS label.' }
        $labels.Add([Text.Encoding]::UTF8.GetString($Data, $pos, $length))
        $pos += $length
    }
    throw 'DNS compression loop.'
}
function New-Query([string]$Name, [int]$Type, [bool]$Unicast) {
    $bytes = New-Object 'System.Collections.Generic.List[byte]'
    $bytes.AddRange([byte[]](0,0,0,0,0,1,0,0,0,0,0,0))
    foreach ($label in $Name.TrimEnd('.').Split('.')) {
        $part = [Text.Encoding]::UTF8.GetBytes($label)
        $bytes.Add([byte]$part.Length)
        $bytes.AddRange($part)
    }
    $bytes.AddRange([byte[]](0,0,$Type))
    if ($Unicast) { $bytes.AddRange([byte[]](128,1)) }
    else { $bytes.AddRange([byte[]](0,1)) }
    return ,$bytes.ToArray()
}

if (-not $LocalIP) {
    $wifi = @([Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
        Where-Object { $_.OperationalStatus -eq 'Up' -and
                       $_.NetworkInterfaceType -eq 'Wireless80211' })
    $addresses = @($wifi | ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
        Where-Object { $_.Address.AddressFamily -eq 'InterNetwork' -and
                       -not $_.Address.ToString().StartsWith('169.254.') } |
        ForEach-Object { $_.Address.ToString() } | Select-Object -Unique)
    if ($addresses.Count -ne 1) {
        Write-Host 'Cannot choose one Wi-Fi IPv4 address. Run ipconfig, then:'
        Write-Host '.\Ballu-Discover.ps1 -LocalIP YOUR_WIFI_IPV4'
        exit 1
    }
    $LocalIP = $addresses[0]
}
$address = [Net.IPAddress]::Parse($LocalIP)
if ($address.AddressFamily -ne 'InterNetwork') { throw 'An IPv4 Wi-Fi address is required.' }
$group = [Net.IPAddress]::Parse('224.0.0.251')
$target = New-Object Net.IPEndPoint($group, 5353)
$client = New-Object Net.Sockets.UdpClient([Net.Sockets.AddressFamily]::InterNetwork)
$unicast = $false
$devices = @{}
$queried = @{}
try {
    $client.ExclusiveAddressUse = $false
    $client.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::Socket,
        [Net.Sockets.SocketOptionName]::ReuseAddress, $true)
    try {
        $client.Client.Bind((New-Object Net.IPEndPoint([Net.IPAddress]::Any, 5353)))
    } catch {
        $client.Close()
        $client = New-Object Net.Sockets.UdpClient([Net.Sockets.AddressFamily]::InterNetwork)
        $client.Client.Bind((New-Object Net.IPEndPoint($address, 0)))
        $unicast = $true
        Write-Host 'Port 5353 is busy; requesting unicast replies. Discovery may be incomplete.'
    }
    if (-not $unicast) { $client.JoinMulticastGroup($group, $address) }
    $client.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::IP,
        [Net.Sockets.SocketOptionName]::MulticastInterface, $address.GetAddressBytes())
    $client.Client.ReceiveTimeout = 500
    Write-Host "Read-only Syncleo discovery on Wi-Fi $LocalIP ($Seconds seconds)..."
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $lastQuery = -5
    while ($timer.Elapsed.TotalSeconds -lt $Seconds) {
        if ($timer.Elapsed.TotalSeconds - $lastQuery -ge 3) {
            $query = New-Query '_syncleo._udp.local' 12 $unicast
            [void]$client.Send($query, $query.Length, $target)
            $lastQuery = $timer.Elapsed.TotalSeconds
        }
        $remote = New-Object Net.IPEndPoint([Net.IPAddress]::Any, 0)
        try { [byte[]]$data = $client.Receive([ref]$remote) }
        catch [Net.Sockets.SocketException] {
            if ($_.Exception.SocketErrorCode -eq 'TimedOut') { continue }
            throw
        }
        try {
            if ($data.Length -lt 12 -or ($data[2] -band 128) -eq 0) { continue }
            $questions = Read-U16 $data 4
            $count = (Read-U16 $data 6) + (Read-U16 $data 8) + (Read-U16 $data 10)
            $offset = 12
            for ($i=0; $i -lt $questions; $i++) {
                $null = Read-DnsName $data ([ref]$offset)
                $offset += 4
            }
            for ($i=0; $i -lt $count; $i++) {
                $owner = Read-DnsName $data ([ref]$offset)
                $type = Read-U16 $data $offset
                $length = Read-U16 $data ($offset + 8)
                $offset += 10
                $end = $offset + $length
                if ($end -gt $data.Length) { throw 'Short resource record.' }
                if ($type -eq 12 -and $owner -eq '_syncleo._udp.local') {
                    $ptr = $offset
                    $instance = Read-DnsName $data ([ref]$ptr)
                    if (-not $queried.ContainsKey($instance)) {
                        $queried[$instance] = $true
                        foreach ($qtype in @(16,33)) {
                            $query = New-Query $instance $qtype $unicast
                            [void]$client.Send($query, $query.Length, $target)
                        }
                    }
                }
                if ($owner.EndsWith('._syncleo._udp.local') -and $type -in @(16,33)) {
                    if (-not $devices.ContainsKey($owner)) {
                        $devices[$owner] = @{ IP=$remote.Address.ToString(); UDPPort='?';
                            DeviceType='?'; Firmware='?'; Protocol='?'; PublicKeyAdvertised='no' }
                    }
                    $device = $devices[$owner]
                    if ($type -eq 33 -and $length -ge 6) {
                        $device.UDPPort = Read-U16 $data ($offset + 4)
                    }
                    if ($type -eq 16) {
                        $pos = $offset
                        while ($pos -lt $end) {
                            $n = [int]$data[$pos]; $pos++
                            if ($pos + $n -gt $end) { throw 'Short TXT field.' }
                            $txt = [Text.Encoding]::UTF8.GetString($data, $pos, $n)
                            $pos += $n
                            # Whitelist output: never print tokens or arbitrary TXT records.
                            $kv = $txt -split '=', 2
                            if ($kv.Length -ne 2) { continue }
                            $v = $kv[1]
                            if ($kv[0] -eq 'public') { $device.PublicKeyAdvertised = 'yes' }
                            if ($v -notmatch '^[a-zA-Z0-9._-]{1,24}$') { continue }
                            switch ($kv[0]) {
                                'devtype' { $device.DeviceType = $v }
                                'firmware' { $device.Firmware = $v }
                                'protocol' { $device.Protocol = $v }
                            }
                        }
                    }
                }
                $offset = $end
            }
        } catch {
            # Ignore unrelated/malformed LAN responses; do not print raw network data.
        }
    }
} finally { $client.Close() }
if ($devices.Count -eq 0) {
    Write-Host 'No Syncleo service found. This does NOT prove local control is unsupported.'
    Write-Host 'Check the same Wi-Fi, VPN, client isolation and the Windows private-network firewall rule.'
} else {
    $devices.Values | ForEach-Object { [pscustomobject]$_ } |
        Format-Table IP,UDPPort,DeviceType,Firmware,Protocol,PublicKeyAdvertised -AutoSize
}
