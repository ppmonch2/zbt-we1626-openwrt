/* Compact production crypto backend; protocol framing stays shared with OpenSSL. */
#include <mbedtls/cipher.h>
#include <mbedtls/ecdh.h>
#include <fcntl.h>
#include <errno.h>
#include <mbedtls/sha256.h>
#include <mbedtls/platform_util.h>
#include <mbedtls/version.h>
#define OPENSSL_cleanse mbedtls_platform_zeroize

static int aes(int encrypt, const unsigned char *key, const unsigned char *iv,
        const unsigned char *in, int len, unsigned char *out, int padding) {
    mbedtls_cipher_context_t c; size_t n=0,m=0; int ok=0;
    mbedtls_cipher_init(&c);
    const mbedtls_cipher_info_t *info=mbedtls_cipher_info_from_type(MBEDTLS_CIPHER_AES_128_CBC);
    if(info && mbedtls_cipher_setup(&c,info)==0 &&
       mbedtls_cipher_setkey(&c,key,128,encrypt?MBEDTLS_ENCRYPT:MBEDTLS_DECRYPT)==0 &&
       mbedtls_cipher_set_padding_mode(&c,padding?MBEDTLS_PADDING_PKCS7:MBEDTLS_PADDING_NONE)==0 &&
       mbedtls_cipher_set_iv(&c,iv,16)==0 && mbedtls_cipher_reset(&c)==0 &&
       mbedtls_cipher_update(&c,in,(size_t)len,out,&n)==0 &&
       mbedtls_cipher_finish(&c,out+n,&m)==0) ok=(int)(n+m);
    mbedtls_cipher_free(&c); return ok;
}
static int os_random(void *unused, unsigned char *out, size_t len) {
    (void)unused; int fd=open("/dev/urandom",O_RDONLY);if(fd<0)return -1;
    size_t done=0;
    while(done<len) {
        ssize_t n=read(fd,out+done,len-done);
        if(n<0 && errno==EINTR)continue;
        if(n<=0){close(fd);return -1;}
        done+=(size_t)n;
    }
    close(fd);return 0;
}
static int keys(const unsigned char *wirepublic, unsigned char *ourpublic) {
    mbedtls_ecp_group grp; mbedtls_ecp_point q,remote;
    mbedtls_mpi d,z;
    unsigned char raw[32],shared[32],digest[32]; size_t size=0; int ok=0;
    mbedtls_ecp_group_init(&grp);mbedtls_ecp_point_init(&q);mbedtls_ecp_point_init(&remote);
    mbedtls_mpi_init(&d);mbedtls_mpi_init(&z);
    if(mbedtls_ecp_group_load(&grp,MBEDTLS_ECP_DP_CURVE25519)!=0 ||
       mbedtls_ecp_gen_keypair(&grp,&d,&q,os_random,NULL)!=0 ||
       mbedtls_ecp_point_write_binary(&grp,&q,MBEDTLS_ECP_PF_UNCOMPRESSED,&size,raw,32)!=0 || size!=32) goto end;
    for(int i=0;i<32;i++) {ourpublic[i]=raw[31-i];shared[i]=wirepublic[31-i];}
    if(mbedtls_ecp_point_read_binary(&grp,&remote,shared,32)!=0 ||
       mbedtls_ecdh_compute_shared(&grp,&z,&remote,&d,os_random,NULL)!=0 ||
       mbedtls_mpi_write_binary_le(&z,shared,32)!=0) goto end;
    unsigned char nonzero=0;for(int i=0;i<32;i++)nonzero|=shared[i];if(!nonzero)goto end;
    for(int i=0;i<32;i++)raw[i]=shared[31-i];
#if MBEDTLS_VERSION_MAJOR >= 3
    if(mbedtls_sha256(raw,32,digest,0)!=0) goto end;
#else
    if(mbedtls_sha256_ret(raw,32,digest,0)!=0) goto end;
#endif
    memcpy(keyin,digest,16);memcpy(keyout,digest+16,16);ok=1;
end:
    mbedtls_platform_zeroize(raw,sizeof(raw));mbedtls_platform_zeroize(shared,sizeof(shared));
    mbedtls_platform_zeroize(digest,sizeof(digest));
    mbedtls_ecp_group_free(&grp);mbedtls_ecp_point_free(&q);mbedtls_ecp_point_free(&remote);
    mbedtls_mpi_free(&d);mbedtls_mpi_free(&z);
    return ok;
}
