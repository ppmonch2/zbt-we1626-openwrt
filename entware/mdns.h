/* Targeted, read-only mDNS discovery. No appliance settings are changed. */
#include <netinet/in.h>
#include <ctype.h>
static unsigned dns16(const unsigned char *b) {return b[0]*256u+b[1];}
static int dnsname(const unsigned char *b,int n,int *offset,char *out,int cap) {
 int p=*offset,next=-1,k=0;
 for(int tries=0;tries<128;tries++) {
  if(p<0||p>=n)return 0;
  int z=b[p++];
  if((z&192)==192){if(p>=n)return 0;if(next<0)next=p+1;p=((z&63)<<8)|b[p];continue;}
  if(z>63||p+z>n)return 0;
  if(!z){*offset=next<0?p:next;out[k]=0;return 1;}
  if(k){if(k+1>=cap)return 0;out[k++]='.';}
  if(k+z>=cap)return 0;
  for(int j=0;j<z;j++)out[k++]=(char)tolower(b[p++]);
 }
 return 0;
}
static int dnsquestion(unsigned char *b,int pos,const char *name,int type) {
 const char *s=name;
 while(*s){const char *end=strchr(s,'.');int n=end?(int)(end-s):(int)strlen(s);
  b[pos++]=(unsigned char)n;memcpy(b+pos,s,(size_t)n);pos+=n;if(!end)break;s=end+1;}
 b[pos++]=0;b[pos++]=0;b[pos++]=(unsigned char)type;b[pos++]=0;b[pos++]=1;return pos;
}
static int txt_matches_device(const unsigned char *b,int start,int end,const char *id) {
 while(start<end){int n=b[start++];if(start+n>end)return 0;
  const unsigned char *value=NULL;int len=0;
  if(n>8&&!memcmp(b+start,"macaddr=",8)){value=b+start+8;len=n-8;}
  else if(n>4&&!memcmp(b+start,"mac=",4)){value=b+start+4;len=n-4;}
  if(value){char clean[13];int k=0;for(int j=0;j<len;j++)if(value[j]!=':'){if(k>=12)return 0;clean[k++]=(char)tolower(value[j]);}clean[k]=0;
   if(k==12&&!strncmp(clean,id,12))return 1;}
  start+=n;
 }
 return 0;
}
static int mdns_discover(const char *lan,const char *id) {
 if(strlen(id)!=12)return fail("invalid_device_id");
 for(int i=0;i<12;i++)if(!isxdigit((unsigned char)id[i]))return fail("invalid_device_id");
 struct in_addr local;if(inet_pton(AF_INET,lan,&local)!=1)return fail("invalid_lan_ip");
 char wanted[100],selected[256];snprintf(wanted,sizeof(wanted),"%s._syncleo._udp.local",id);
 for(char *s=wanted;*s;s++)*s=(char)tolower((unsigned char)*s);
 snprintf(selected,sizeof(selected),"%s",wanted);
 int fd=socket(AF_INET,SOCK_DGRAM,0);if(fd<0)return fail("discovery_socket_failed");
 int yes=1;setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
 struct sockaddr_in bindaddr={.sin_family=AF_INET,.sin_port=htons(5353),.sin_addr={.s_addr=htonl(INADDR_ANY)}};
 struct ip_mreq group={.imr_interface=local};inet_pton(AF_INET,"224.0.0.251",&group.imr_multiaddr);
 if(bind(fd,(struct sockaddr*)&bindaddr,sizeof(bindaddr))||
    setsockopt(fd,IPPROTO_IP,IP_ADD_MEMBERSHIP,&group,sizeof(group))||
    setsockopt(fd,IPPROTO_IP,IP_MULTICAST_IF,&local,sizeof(local))) {close(fd);return fail("discovery_bind_failed");}
 unsigned char query[300]={0,0,0,0,0,3};int qn=dnsquestion(query,12,wanted,16);qn=dnsquestion(query,qn,wanted,33);qn=dnsquestion(query,qn,"_syncleo._udp.local",12);
 struct sockaddr_in dest={.sin_family=AF_INET,.sin_port=htons(5353),.sin_addr=group.imr_multiaddr};
 unsigned char buf[CAP];char publickey[65]="",host[256]="",ip[INET_ADDRSTRLEN]="";
 struct {char name[256];struct in_addr addr;} addresses[16];int address_count=0;
 int port=0,protocol=0,type=0,curve=0;double deadline=now()+3.2,nextsend=0;
 while(now()<deadline) {
  if(now()>=nextsend){sendto(fd,query,(size_t)qn,0,(struct sockaddr*)&dest,sizeof(dest));nextsend=now()+.7;}
  struct pollfd pollfd={fd,POLLIN,0};if(poll(&pollfd,1,150)<=0)continue;
  struct sockaddr_in from;socklen_t fromlen=sizeof(from);int n=(int)recvfrom(fd,buf,sizeof(buf),0,(struct sockaddr*)&from,&fromlen);
  if(n<12||from.sin_port!=htons(5353)||!(buf[2]&128))continue;
  int p=12,valid=1;unsigned questions=dns16(buf+4),count=dns16(buf+6)+dns16(buf+8)+dns16(buf+10);
  char owner[256];
  for(unsigned i=0;i<questions;i++){if(!dnsname(buf,n,&p,owner,sizeof(owner))||p+4>n){valid=0;break;}p+=4;}
  if(!valid)continue;
  for(unsigned i=0;i<count;i++) {
   if(!dnsname(buf,n,&p,owner,sizeof(owner))||p+10>n)break;
   int kind=(int)dns16(buf+p),len=(int)dns16(buf+p+8),start=p+10,end=start+len;
   int alive=buf[p+4]||buf[p+5]||buf[p+6]||buf[p+7];p=end;if(end>n)break;if(!alive)continue;
   if(kind==1&&len==4&&address_count<16){snprintf(addresses[address_count].name,256,"%s",owner);memcpy(&addresses[address_count++].addr,buf+start,4);}
   if(kind==12&&!strcmp(owner,"_syncleo._udp.local")) {
    int z=start;char instance[256];if(dnsname(buf,n,&z,instance,sizeof(instance))){
     unsigned char subquery[600]={0,0,0,0,0,2};int sn=dnsquestion(subquery,12,instance,16);sn=dnsquestion(subquery,sn,instance,33);
     sendto(fd,subquery,(size_t)sn,0,(struct sockaddr*)&dest,sizeof(dest));
    }
   }
   if(kind==16&&strstr(owner,"._syncleo._udp.local")&&txt_matches_device(buf,start,end,wanted)&&strcmp(owner,selected)) {
    snprintf(selected,sizeof(selected),"%s",owner);publickey[0]=host[0]=ip[0]=0;port=protocol=type=curve=0;
   }
   if(strcmp(owner,selected))continue;
   if(kind==33&&len>=7){port=(int)dns16(buf+start+4);int z=start+6;dnsname(buf,n,&z,host,sizeof(host));}
   if(kind==16){int z=start;while(z<end){int l=buf[z++];if(z+l>end)break;
     char text[260];memcpy(text,buf+z,(size_t)l);text[l]=0;z+=l;
     if(!strncmp(text,"public=",7)&&strlen(text+7)==64){unsigned char key[32];if(hex(text+7,key,32))memcpy(publickey,text+7,65);}
     if(!strncmp(text,"protocol=",9))protocol=atoi(text+9);
     if(!strncmp(text,"devtype=",8))type=atoi(text+8);
     if(!strncmp(text,"curve=",6))curve=atoi(text+6);
   }}
  }
  for(int j=0;j<address_count;j++)if(!strcmp(addresses[j].name,host))inet_ntop(AF_INET,&addresses[j].addr,ip,sizeof(ip));
  if(host[0]&&!ip[0]){unsigned char aquery[300]={0,0,0,0,0,1};int an=dnsquestion(aquery,12,host,1);sendto(fd,aquery,(size_t)an,0,(struct sockaddr*)&dest,sizeof(dest));}
  if(ip[0]&&publickey[0]&&port>0&&protocol==3&&type==46&&curve==29) {
   printf("{\"ip\":\"%s\",\"port\":%d,\"public_key\":\"%s\",\"protocol\":3,\"devtype\":46}\n",ip,port,publickey);
   close(fd);return 0;
  }
 }
 close(fd);return fail("device_not_found_or_unsupported_mdns_check_same_lan");
}
