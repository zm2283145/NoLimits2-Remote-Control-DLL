#include "../src/native_attraction.h"
#include <thread>
#include <cassert>
#include <cstdio>
static void exact(SOCKET s,uint8_t* b,int size) {
  while(size) {int n=recv(s,(char*)b,size,0);assert(n>0);b+=n;size-=n;}
}
static uint16_t listenLocal(SOCKET& s) {
  s=socket(AF_INET,SOCK_STREAM,0);assert(s!=INVALID_SOCKET);
  sockaddr_in a{};a.sin_family=AF_INET;a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
  assert(bind(s,(sockaddr*)&a,sizeof a)==0);assert(listen(s,1)==0);
  int len=sizeof a;assert(getsockname(s,(sockaddr*)&a,&len)==0);return ntohs(a.sin_port);
}
int main() {
  WSADATA wd;assert(WSAStartup(MAKEWORD(2,2),&wd)==0);
  SOCKET listener;auto port=listenLocal(listener);
  std::thread server([&] {
    SOCKET s=accept(listener,nullptr,nullptr);assert(s!=INVALID_SOCKET);
    for(int step=0;step<3;++step) {
      uint8_t h[9];exact(s,h,9);assert(h[0]=='N'&&h[6]==1);
      assert(h[2]==(step==1?0:30));int length=h[8];assert(length==(step==1?0:1));
      uint8_t body[2];exact(s,body,length+1);assert(body[length]=='L');
      if(length) assert(body[0]==(step==0?1:0));
      const uint8_t reply[]={'N',0,1,0,0,0,1,0,0,'L'};
      assert(send(s,(const char*)reply,3,0)==3);Sleep(10);
      assert(send(s,(const char*)reply+3,7,0)==7);
    }
    uint8_t b;assert(recv(s,(char*)&b,1,0)==0);closesocket(s);
  });
  NativeAttractionConnection connection;std::string error;
  assert(connection.set(port,true,error));assert(connection.keepAlive(error));
  assert(connection.set(port,false,error));server.join();closesocket(listener);
  port=listenLocal(listener);
  std::thread refusal([&] {
    SOCKET s=accept(listener,nullptr,nullptr);uint8_t request[11];exact(s,request,11);
    const uint8_t reply[]={'N',0,2,0,0,0,1,0,7,'r','e','f','u','s','e','d','L'};
    assert(send(s,(const char*)reply,sizeof reply,0)==sizeof reply);
    uint8_t b;assert(recv(s,(char*)&b,1,0)==0);closesocket(s);
  });
  assert(!connection.set(port,true,error));assert(error=="refused");refusal.join();closesocket(listener);
  WSACleanup();std::puts("PASS persistent native socket, fragmented replies, idle heartbeat, explicit restoration and refusal cleanup");
}
