"""Independent encrypted UDP peer for the native C client. No physical heaters."""
import os
import shlex
import hashlib
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class SyncleoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        except ImportError:
            raise unittest.SkipTest('cryptography is required for the independent peer')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = os.environ.get('SYNCLEO_TEST_BINARY',str(Path(cls.tmp.name) / 'syncleo'))
        if os.environ.get('SYNCLEO_TEST_BINARY'):
            return
        subprocess.run(['gcc','-Os','-std=c99','-Wall','-Wextra','-Werror',
                        *shlex.split(os.environ.get('SYNCLEO_CFLAGS','')),
                        str(ROOT / 'native/syncleo/main.c'),'-o',cls.binary,
                        *shlex.split(os.environ.get('SYNCLEO_LIBS','-lcrypto'))],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def exchange(self, action='read', reject_token=False, forged_ack=False):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        private = X25519PrivateKey.generate()
        public = private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)[::-1]
        token = bytes.fromhex('1234567890abcdef1234567890abcdef')
        sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1',0));sock.settimeout(.2)
        endpoint = sock.getsockname()
        stop = threading.Event(); errors=[]; commands=[]
        def crypt(key,iv,data,encrypt,pad=True):
            if encrypt and pad:
                p=padding.PKCS7(128).padder();data=p.update(data)+p.finalize()
            c=Cipher(algorithms.AES(key),modes.CBC(iv))
            op=c.encryptor() if encrypt else c.decryptor()
            data=op.update(data)+op.finalize()
            if not encrypt and pad:
                p=padding.PKCS7(128).unpadder();data=p.update(data)+p.finalize()
            return data
        def peer():
            try:
                while not stop.is_set():
                    try: packet,addr=sock.recvfrom(2048)
                    except socket.timeout: continue
                    self.assertEqual(packet[:5],bytes([0,1,49,0,0]))
                    remote=X25519PublicKey.from_public_bytes(packet[5:37][::-1])
                    shared=private.exchange(remote)[::-1]
                    digest=hashlib.sha256(shared).digest();keyin,keyout=digest[:16],digest[16:]
                    got=crypt(keyout,keyin,packet[37:],False,False)
                    self.assertEqual(got,token)
                    if reject_token: continue
                    def encode(seq,kind,payload):
                        i,j=seq&15,seq>>4
                        cipher=crypt(keyin[i:]+keyin[:i],keyout[j:]+keyout[:j],bytes([seq])+payload,True)
                        return struct.pack('<BBH',seq,kind,len(cipher))+cipher
                    sock.sendto(encode(0,1,bytes([0,3,0,1,43,0])+token),addr)
                    sock.sendto(encode(4,1,bytes([2,18,0])),addr)
                    sock.sendto(encode(5,1,bytes([20,21,50])),addr)
                    while not stop.is_set():
                        try: frame,addr=sock.recvfrom(2048)
                        except socket.timeout: continue
                        seq,kind,n=struct.unpack('<BBH',frame[:4])
                        self.assertEqual(len(frame),n+4)
                        i,j=seq&15,seq>>4
                        plain=crypt(keyout[i:]+keyout[:i],keyin[j:]+keyin[:j],frame[4:],False)
                        self.assertEqual(plain[0],seq)
                        if kind==1:
                            commands.append(plain[1:])
                            self.assertEqual(plain[1:],bytes([1,0]) if action=='mode' else bytes([2,22,0]))
                            if forged_ack:
                                sock.sendto(bytes([1,0,0,0]),addr)
                            else:
                                sock.sendto(encode(1,0,b''),addr)
                                sock.sendto(encode(6,1,bytes([1,0]) if action=='mode' else bytes([2,22,0])),addr)
            except Exception as e: errors.append(e)
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        config=f'127.0.0.1\n{endpoint[1]}\n{public.hex()}\n{token.hex()}\n'
        args=[*shlex.split(os.environ.get('SYNCLEO_RUNNER','')),self.binary,action]+(['22'] if action=='set' else ['0'] if action=='mode' else [])
        try:
            result=subprocess.run(args,input=config,text=True,capture_output=True,timeout=12)
        finally:
            stop.set();worker.join(timeout=1);sock.close()
        if errors: raise errors[0]
        return result,commands

    def test_read_authenticates_without_appliance_commands(self):
        r,commands=self.exchange()
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertTrue(data['encrypted_session'])
        self.assertEqual(data['reported_target'],18)
        self.assertEqual(data['current_temperature'],21.5)
        self.assertEqual(commands,[])
        self.assertFalse(data['heating_confirmed'])

    def test_set_requires_encrypted_ack_and_reports_echo(self):
        r,commands=self.exchange('set')
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertTrue(data['command_acknowledged'])
        self.assertTrue(data['setpoint_reported'])
        self.assertIn(bytes([2,22,0]),commands)
        self.assertFalse(data['heating_confirmed'])

    def test_plaintext_ack_is_not_accepted(self):
        r,_=self.exchange('set',forged_ack=True)
        self.assertEqual(r.returncode,2)
        self.assertFalse(json.loads(r.stdout)['command_acknowledged'])

    def test_handshake_timeout_never_sends_heating_command(self):
        r,commands=self.exchange('set',reject_token=True)
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(commands,[])
        self.assertNotIn('1234567890abcdef',r.stderr+r.stdout)

    def test_power_off_requires_acknowledged_mode_command(self):
        r,commands=self.exchange('mode')
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertTrue(data['command_acknowledged'])
        self.assertTrue(data['state_echo_matches_request'])
        self.assertEqual(data['device_mode'],0)
        self.assertIn(bytes([1,0]),commands)
        self.assertFalse(data['heating_confirmed'])
