/* Syncleo v3 minimal endpoint client. Based on protocol documented by
 * DeKaN/pysyncleo (MIT); see THIRD_PARTY.md. No time sync, DNS changes or pairing.
 * Credentials are read from stdin (IP, port, public hex, token hex), never argv.
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#ifndef HEATBRIDGE_MBEDTLS
#include <openssl/crypto.h>
#include <openssl/evp.h>
#endif
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define CAP 2048
static unsigned char keyin[16], keyout[16];
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static int fail(const char *s) { fprintf(stderr,"%s\n",s); return 1; }
static int hex(const char *s, unsigned char *out, size_t n) {
    if(strlen(s)!=2*n) return 0;
    for(size_t i=0;i<n;i++) { unsigned v=0; for(int j=0;j<2;j++) {
        unsigned char c=s[2*i+j]; int d=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;
        if(d<0) return 0;
        v=(v<<4)|(unsigned)d;
    } out[i]=(unsigned char)v; } return 1;
}
static int line(char *s, size_t n) {
    if(!fgets(s,(int)n,stdin)) return 0;
    size_t k=strcspn(s,"\r\n"); if(k==n-1) return 0; s[k]=0; return 1;
}
#ifdef HEATBRIDGE_MBEDTLS
#include "crypto_mbedtls.h"
#else
static int aes(int encrypt, const unsigned char *key, const unsigned char *iv,
        const unsigned char *in, int len, unsigned char *out, int padding) {
    EVP_CIPHER_CTX *c=EVP_CIPHER_CTX_new(); int n=0,m=0,ok=0;
    if(c && EVP_CipherInit_ex(c,EVP_aes_128_cbc(),NULL,key,iv,encrypt)==1 &&
       EVP_CIPHER_CTX_set_padding(c,padding)==1 &&
       EVP_CipherUpdate(c,out,&n,in,len)==1 && EVP_CipherFinal_ex(c,out+n,&m)==1) ok=n+m;
    EVP_CIPHER_CTX_free(c); return ok;
}
static int keys(const unsigned char *wirepublic, unsigned char *ourpublic) {
    EVP_PKEY_CTX *gen=EVP_PKEY_CTX_new_id(EVP_PKEY_X25519,NULL), *derive=NULL;
    EVP_PKEY *local=NULL,*remote=NULL; unsigned char raw[32],shared[32],digest[32];
    size_t size=32; int ok=0;
    for(int i=0;i<32;i++) raw[i]=wirepublic[31-i];
    remote=EVP_PKEY_new_raw_public_key(EVP_PKEY_X25519,NULL,raw,32);
    if(!gen || !remote || EVP_PKEY_keygen_init(gen)!=1 || EVP_PKEY_keygen(gen,&local)!=1) goto end;
    if(EVP_PKEY_get_raw_public_key(local,raw,&size)!=1 || size!=32) goto end;
    for(int i=0;i<32;i++) ourpublic[i]=raw[31-i];
    derive=EVP_PKEY_CTX_new(local,NULL); size=32;
    if(!derive || EVP_PKEY_derive_init(derive)!=1 || EVP_PKEY_derive_set_peer(derive,remote)!=1 ||
       EVP_PKEY_derive(derive,shared,&size)!=1 || size!=32) goto end;
    for(int i=0;i<32;i++) raw[i]=shared[31-i];
    if(EVP_Digest(raw,32,digest,NULL,EVP_sha256(),NULL)!=1) goto end;
    memcpy(keyin,digest,16); memcpy(keyout,digest+16,16); ok=1;
end:
    OPENSSL_cleanse(raw,sizeof(raw)); OPENSSL_cleanse(shared,sizeof(shared)); OPENSSL_cleanse(digest,sizeof(digest));
    EVP_PKEY_CTX_free(gen); EVP_PKEY_CTX_free(derive); EVP_PKEY_free(local); EVP_PKEY_free(remote); return ok;
}
#endif
static void rotate(const unsigned char *in, unsigned char *out, int n) { for(int i=0;i<16;i++) out[i]=in[(i+n)%16]; }
static int frame(unsigned char seq,unsigned char type,const unsigned char *payload,int len,unsigned char *out) {
    unsigned char k[16],iv[16],plain[CAP];
    if(len<0 || len>CAP-32) return 0;
    rotate(keyout,k,seq&15); rotate(keyin,iv,seq>>4);
    plain[0]=seq; if(len) memcpy(plain+1,payload,(size_t)len);
    int n=aes(1,k,iv,plain,len+1,out+4,1); OPENSSL_cleanse(plain,sizeof(plain));
    if(!n) return 0;
    out[0]=seq;out[1]=type;out[2]=(unsigned char)n;out[3]=(unsigned char)(n>>8);return n+4;
}
static int decode(const unsigned char *in,int len,unsigned char *plain) {
    unsigned char k[16],iv[16]; if(len<20) return -1;
    int n=in[2]+256*in[3]; if(n!=len-4 || n%16 || n>CAP-16) return -1;
    rotate(keyin,k,in[0]&15);rotate(keyout,iv,in[0]>>4);
    int size=aes(0,k,iv,in+4,n,plain,1);
    if(size<1 || plain[0]!=in[0]) return -1;
    memmove(plain,plain+1,(size_t)(size-1));return size-1;
}
static int send_frame(int fd,unsigned char seq,unsigned char type,const unsigned char *payload,int len) {
    unsigned char out[CAP];int n=frame(seq,type,payload,len,out);
    return n && send(fd,out,(size_t)n,0)==n;
}
static int receive(int fd,unsigned char *buf,int timeout) {
    struct pollfd p={fd,POLLIN,0}; if(poll(&p,1,timeout)<=0) return -1;
    return (int)recv(fd,buf,CAP,0);
}
static int port_number(const char *s) {
    char *end; long n=strtol(s,&end,10); return *s && !*end && n>0 && n<=65535?(int)n:0;
}
int main(int argc,char **argv) {
    int setting=0,temp=0;
    if(argc==2 && !strcmp(argv[1],"read")) setting=0;
    else if(argc==3 && !strcmp(argv[1],"set")) {
        char *end;long t=strtol(argv[2],&end,10);
        if(!*argv[2] || *end || t<5 || t>30) return fail("invalid_temperature");
        setting=1;temp=(int)t;
    } else return fail("usage: heatbridge-syncleo read | set INTEGER_5_TO_30");
    char ip[64],porttext[16],publictext[80],tokentext[48];
    unsigned char publickey[32],token[16],handshake[53]={0,1,49,0,0};
    if(!line(ip,sizeof(ip)) || !line(porttext,sizeof(porttext)) || !line(publictext,sizeof(publictext)) ||
       !line(tokentext,sizeof(tokentext)) || !hex(publictext,publickey,32) || !hex(tokentext,token,16)) return fail("invalid_connection_settings");
    int port=port_number(porttext);
    struct sockaddr_in remote={.sin_family=AF_INET,.sin_port=htons((uint16_t)port)};
    if(!port || inet_pton(AF_INET,ip,&remote.sin_addr)!=1) return fail("invalid_endpoint");
    if(!keys(publickey,handshake+5) || aes(1,keyout,keyin,token,16,handshake+37,0)!=16) return fail("crypto_initialization_failed");
    OPENSSL_cleanse(token,sizeof(token)); OPENSSL_cleanse(tokentext,sizeof(tokentext));
    int fd=socket(AF_INET,SOCK_DGRAM,0);if(fd<0) return fail("socket_failed");
    if(connect(fd,(struct sockaddr *)&remote,sizeof(remote))) { close(fd);return fail("connect_failed"); }
    unsigned char buf[CAP],plain[CAP];int connected=0;
    for(int attempt=0;attempt<3 && !connected;attempt++) {
        if(send(fd,handshake,sizeof(handshake),0)!=(int)sizeof(handshake)) break;
        double deadline=now()+1.0;
        while(now()<deadline) {
            int n=receive(fd,buf,100),size=n>0?decode(buf,n,plain):-1;
            if(size>=22 && buf[1]==1 && plain[0]==0 && plain[1]==3 && plain[2]==0) {
                if(!send_frame(fd,buf[0],0,NULL,0)) break;
                connected=1;break;
            }
        }
    }
    OPENSSL_cleanse(handshake,sizeof(handshake));
    if(!connected) { close(fd);return fail("encrypted_handshake_failed_check_token_and_current_public_key"); }
    int target=-1,current_valid=0,mode=-1,ack=0,echo=0;
    double current=0,deadline=now()+(setting?6:3),lastsend=0;
    while(now()<deadline) {
        if(setting && !ack && now()-lastsend>=1.0) {
            unsigned char command[3]={2,(unsigned char)temp,0};
            if(!send_frame(fd,1,1,command,3)) break;
            lastsend=now();
        }
        int n=receive(fd,buf,100),size=n>0?decode(buf,n,plain):-1;
        if(size<0) continue;
        if(buf[1]==0 && buf[0]==1 && setting) { ack=1;continue; }
        if(buf[1]==255) { close(fd);return fail("device_rejected_command"); }
        if(buf[1]!=1 || size<1) continue;
        if(!send_frame(fd,buf[0],0,NULL,0)) break;
        if(plain[0]==2 && size==3 && plain[1]<=35 && plain[2]==0) {
            target=plain[1]; if(setting && target==temp) echo=1;
        }
        if(plain[0]==1 && size==2) mode=plain[1];
        if(plain[0]==20 && size==3 && (plain[2]&127)<100) {
            current=plain[1]+(plain[2]&127)/100.0;if(plain[2]&128) current=-current;current_valid=1;
        }
        if(setting && ack && echo) break;
    }
    close(fd);OPENSSL_cleanse(keyin,sizeof(keyin));OPENSSL_cleanse(keyout,sizeof(keyout));
    printf("{\"ok\":true,\"encrypted_session\":true,\"command_acknowledged\":%s,\"setpoint_reported\":%s,\"reported_target\":",ack?"true":"false",echo?"true":"false");
    if(target>=0) printf("%d",target);else printf("null");
    printf(",\"current_temperature\":");if(current_valid) printf("%.2f",current);else printf("null");
    printf(",\"device_mode\":");if(mode>=0) printf("%d",mode);else printf("null");
    printf(",\"heating_confirmed\":false}\n");
    return setting && !ack?2:0;
}
