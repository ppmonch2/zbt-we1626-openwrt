Windows: connect this PC to the same Wi-Fi network as the Ballu heater.
Extract the ZIP, open PowerShell in this folder, and run:

powershell -NoProfile -ExecutionPolicy Bypass -File .\Ballu-Discover.ps1

If Windows Firewall asks, allow access on your trusted private network only.
The script only performs local mDNS discovery. It does not control heaters,
change settings or need the device token. Send the resulting table or screenshot.
No results does not prove the device lacks a local protocol.
