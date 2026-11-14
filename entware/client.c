#define _DEFAULT_SOURCE
#define main syncleo_main
#include "../native/syncleo/main.c"
#undef main
#include "mdns.h"
int main(int argc,char **argv) {
 if(argc==4&&!strcmp(argv[1],"discover"))return mdns_discover(argv[2],argv[3]);
 return syncleo_main(argc,argv);
}
