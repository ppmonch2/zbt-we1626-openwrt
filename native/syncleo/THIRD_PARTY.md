Protocol reference: https://github.com/DeKaN/pysyncleo (encoding.py, transport.py,
commands.py). The C client implements the same X25519 byte ordering, SHA-256 key
split, rotated AES-128-CBC framing and temperature encoding. Upstream's MIT
license is included in LICENSE.pysyncleo.

Unlike the reference transport, read mode sends no time synchronization,
diagnostic configuration, MQTT settings or appliance commands. It only initiates
an encrypted session and acknowledges received frames. Set mode only sends the
target-temperature command. No mode or power command is implemented.

The wire protocol uses CBC without a separate MAC. This client checks padding,
sequence and frame length, and accepts traffic only from the connected UDP peer.
Do not expose it to untrusted networks. Successful mocked tests do not prove
compatibility with a physical device.
