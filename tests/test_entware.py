"""Exercise the USB app with fake device I/O, never using owner credentials."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
TOKEN='a1'*16
class EntwareApiTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);(self.root/'etc/heatbridge').mkdir(parents=True);(self.root/'bin').mkdir()
  self.cfg=self.root/'etc/heatbridge/config.json'
  self.cfg.write_text(json.dumps({'lan_ip':'192.168.1.1','heaters':{'h1':{'name':'Window','temp':18,'device_id':'aabbccddeeff','token':TOKEN,'verified':False}}}))
  worker=self.root/'bin/heatbridge-device'
  worker.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['HEATBRIDGE_PREFIX'])
with (p/'calls').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if (p/'fail').exists():sys.exit(1)
print(json.dumps({'ok':True,'encrypted_session':True,'command_acknowledged':sys.argv[1]!='read','reported_target':22,'current_temperature':21.5,'device_mode':1,'heating_confirmed':False}))
''');worker.chmod(0o755)
 def request(self,payload=None,**extra):
  env=dict(os.environ,HEATBRIDGE_PREFIX=str(self.root),HTTP_HOST='192.168.1.1:8765',REQUEST_METHOD='GET' if payload is None else 'POST',CONTENT_TYPE='application/json',HTTP_X_HEATBRIDGE='1')
  body='' if payload is None else json.dumps(payload)
  env['CONTENT_LENGTH']=str(len(body.encode()));env.update(extra)
  run=subprocess.run(['sh',str(ROOT/'entware/www/cgi-bin/api')],input=body,text=True,env=env,capture_output=True)
  self.assertEqual(run.returncode,0,run.stderr)
  return json.loads(run.stdout.split('\n\n',1)[1])
 def test_get_does_not_expose_token_or_device_id(self):
  data=self.request();self.assertTrue(data['ok'])
  self.assertNotIn(TOKEN,json.dumps(data));self.assertNotIn('aabbccddeeff',json.dumps(data))
  self.assertFalse((self.root/'calls').exists())
 def test_rejects_cross_site_and_form_requests(self):
  for args in [{'HTTP_ORIGIN':'https://evil.example'},{'HTTP_HOST':'evil.example:8765'},{'HTTP_X_HEATBRIDGE':''},{'CONTENT_TYPE':'application/x-www-form-urlencoded'}]:
   data=self.request({'action':'probe','id':'h1'},**args);self.assertFalse(data['ok'])
  self.assertFalse((self.root/'calls').exists())
 def test_configure_only_stores_credentials_and_clears_verification(self):
  data=self.request({'action':'configure','id':'h1','name':'Kitchen','device_id':'001122334455','token':TOKEN})
  self.assertTrue(data['ok']);self.assertFalse(data['heaters'][0]['verified'])
  self.assertNotIn(TOKEN,json.dumps(data));self.assertFalse((self.root/'calls').exists())
  self.assertEqual(self.cfg.stat().st_mode&0o777,0o600)
 def test_probe_enables_device_only_after_fresh_encrypted_state(self):
  data=self.request({'action':'probe','id':'h1'})
  self.assertTrue(data['heaters'][0]['verified']);self.assertEqual(data['result']['reported_target'],22)
 def test_failed_read_does_not_verify_device(self):
  (self.root/'fail').touch();data=self.request({'action':'probe','id':'h1'})
  self.assertFalse(data['ok']);self.assertFalse(json.loads(self.cfg.read_text())['heaters']['h1']['verified'])
 def test_failed_command_is_not_saved_as_success(self):
  (self.root/'fail').touch();data=self.request({'action':'set','id':'h1','value':22})
  self.assertIn('may_have_been_applied',data['error']);self.assertEqual(json.loads(self.cfg.read_text())['heaters']['h1']['temp'],18)
 def test_invalid_ids_and_temperatures_do_not_reach_client(self):
  for payload in [{'action':'set','id':'h5','value':22},{'action':'set','id':'h1','value':31},{'action':'mode','id':'h1','value':99},{'action':'configure','id':'h1','name':'x','device_id':'aabbccddeeff','token':'bad'}]:
   self.assertFalse(self.request(payload)['ok'])
  self.assertFalse((self.root/'calls').exists())
 def test_confirmed_setpoint_is_persisted(self):
  data=self.request({'action':'set','id':'h1','value':22})
  self.assertTrue(data['ok']);self.assertEqual(json.loads(self.cfg.read_text())['heaters']['h1']['temp'],22)
class MdnsParserTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();p=Path(cls.tmp.name)
  src=p/'parser.c';src.write_text('#define _DEFAULT_SOURCE\n#define main client_main\n#include "'+str(ROOT/'native/syncleo/main.c')+'"\n#undef main\n#include "'+str(ROOT/'entware/mdns.h')+'"\nint parse(const unsigned char*b,int n,int*pos,char*out,int cap){return dnsname(b,n,pos,out,cap);}\nint match_device(const unsigned char*b,int n,const char*id){return txt_matches_device(b,0,n,id);}\n')
  subprocess.run(['gcc','-shared','-fPIC','-Os',str(src),'-lcrypto','-o',str(p/'parser.so')],check=True)
  cls.lib=ctypes.CDLL(str(p/'parser.so'));cls.lib.parse.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.POINTER(ctypes.c_int),ctypes.c_char_p,ctypes.c_int]
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 def test_valid_compressed_name_and_updated_offset(self):
  data=b'\x03abc\x05local\0\xc0\x00';pos=ctypes.c_int(11);out=ctypes.create_string_buffer(100)
  self.assertEqual(self.lib.parse(data,len(data),ctypes.byref(pos),out,len(out)),1)
  self.assertEqual(out.value,b'abc.local');self.assertEqual(pos.value,13)
 def test_txt_device_identity_is_matched_independently_of_service_name(self):
  self.lib.match_device.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p]
  field=b'macaddr=aa:bb:cc:dd:ee:ff';data=bytes([len(field)])+field
  self.assertEqual(self.lib.match_device(data,len(data),b'aabbccddeeff'),1)
  self.assertEqual(self.lib.match_device(data,len(data),b'000000000000'),0)
  self.assertEqual(self.lib.match_device(data[:-2],len(data)-2,b'aabbccddeeff'),0)
 def test_compression_loops_and_truncated_labels_are_rejected(self):
  for data in [b'\xc0\x00',b'\x05ab',b'\xc0',b'\x40abc']:
   pos=ctypes.c_int(0);out=ctypes.create_string_buffer(100)
   self.assertEqual(self.lib.parse(data,len(data),ctypes.byref(pos),out,len(out)),0)
