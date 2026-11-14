"""Independent crypto-vector checks; no network permission required."""
import hashlib,json,subprocess,tempfile,unittest
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives import padding
ROOT=Path(__file__).resolve().parents[1]
class CryptoTest(unittest.TestCase):
 def test_c_client_matches_independent_crypto_implementation(self):
  private=X25519PrivateKey.from_private_bytes(bytes(range(1,33)))
  public=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)[::-1]
  def aes(key,iv,data,encrypt):
   ctx=Cipher(algorithms.AES(key),modes.CBC(iv))
   if encrypt:
    pad=padding.PKCS7(128).padder();data=pad.update(data)+pad.finalize();op=ctx.encryptor()
    return op.update(data)+op.finalize()
   op=ctx.decryptor();data=op.update(data)+op.finalize();pad=padding.PKCS7(128).unpadder()
   return pad.update(data)+pad.finalize()
  with tempfile.TemporaryDirectory() as tmp:
   src=Path(tmp)/'test.c';exe=Path(tmp)/'test'
   src.write_text('#define main endpoint_main\n#include "'+str(ROOT/'native/syncleo/main.c')+'"\n#undef main\nint main(void) { unsigned char pub[32],own[32],out[CAP],input[CAP],plain[CAP]; char text[4096];\n'
    +'if(!hex("'+public.hex()+'",pub,32)||!keys(pub,own)) return 1;\n'
    +'for(int i=0;i<32;i++) printf("%02x",own[i]); puts("");\n'
    +'unsigned char payload[3]={2,22,0}; int n=frame(33,1,payload,3,out);\n'
    +'for(int i=0;i<n;i++) printf("%02x",out[i]); puts(""); fflush(stdout);\n'
    +'if(!line(text,sizeof(text))) return 2; n=(int)strlen(text)/2; if(!hex(text,input,n))return 3;\n'
    +'int size=decode(input,n,plain);printf("%d\\n",size); for(int i=0;i<size;i++)printf("%02x",plain[i]); puts("");\n'
    +'input[0]^=1; if(decode(input,n,plain)>=0)return 4;\n'
    +'if(decode(input,3,plain)>=0)return 5; return 0; }\n')
   subprocess.run(['gcc','-Os','-std=c99',str(src),'-lcrypto','-o',str(exe)],check=True)
   proc=subprocess.Popen([str(exe)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   own=bytes.fromhex(proc.stdout.readline().strip())
   frame=bytes.fromhex(proc.stdout.readline().strip())
   digest=hashlib.sha256(private.exchange(X25519PublicKey.from_public_bytes(own[::-1]))[::-1]).digest()
   kin,kout=digest[:16],digest[16:]
   self.assertEqual(aes(kout[1:]+kout[:1],kin[2:]+kin[:2],frame[4:],False),bytes([33,2,22,0]))
   cipher=aes(kin[3:]+kin[:3],kout[4:]+kout[:4],bytes([67,20,21,50]),True)
   packet=bytes([67,1,len(cipher),0])+cipher
   stdout,stderr=proc.communicate(packet.hex()+'\n',timeout=5)
   self.assertEqual(proc.returncode,0,stderr)
   self.assertEqual(stdout.splitlines(),['3','141532'])
