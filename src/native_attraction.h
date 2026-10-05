#pragma once
#include <winsock2.h>
#include <string>
#include <vector>
#include "send_all.h"

// Use NL2's documented, license-checked telemetry command. No license flags
// or message-window internals are patched. Call without the game/script lock.
class NativeAttractionConnection {
  SOCKET socket = INVALID_SOCKET;
  uint16_t port=0;
  bool connectTo(uint16_t endpoint,std::string& error) {
    if (socket!=INVALID_SOCKET && port==endpoint) return true;
    close();
    socket=::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (socket == INVALID_SOCKET) { error = "Could not open native telemetry connection"; return false; }
  DWORD timeout = 500;
  setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, (const char*)&timeout, sizeof timeout);
  setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, (const char*)&timeout, sizeof timeout);
  sockaddr_in address{}; address.sin_family = AF_INET;
  address.sin_port = htons(endpoint); address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(socket, (sockaddr*)&address, sizeof address) != 0) {
      error="NL2 native telemetry is unavailable";close();return false;
    }
    port=endpoint;return true;
  }
  bool command(uint16_t type,const uint8_t* payload,unsigned length,std::string& error) {
  bool success = false;
  do {
    std::vector<uint8_t> request={'N',(uint8_t)(type>>8),(uint8_t)type,0,0,0,1,0,(uint8_t)length};
    if (length) request.insert(request.end(),payload,payload+length);
    request.push_back('L');
    if (!SendAll(request.data(), request.size(), [this](const uint8_t* data, int size) {
      return send(socket, (const char*)data, size, 0);
    })) { error = "Native telemetry send failed"; break; }
    auto readExact = [this](uint8_t* data, int size) {
      int offset = 0;
      while (offset < size) {
        int n = recv(socket, (char*)data + offset, size - offset, 0);
        if (n <= 0) return false;
        offset += n;
      }
      return true;
    };
    uint8_t header[9];
    if (!readExact(header, 9) || header[0] != 'N' || header[3] || header[4] || header[5] || header[6] != 1) {
      error = "Native telemetry reply failed"; break;
    }
    unsigned size = (header[7] << 8) | header[8];
    if (size > 4096) { error = "Native telemetry reply too large"; break; }
    std::vector<uint8_t> body(size + 1);
    if (!readExact(body.data(), (int)body.size()) || body.back() != 'L') {
      error = "Native telemetry reply incomplete"; break;
    }
    if (header[1] != 0 || header[2] != 1 || size!=0) {
      error = (header[1] == 0 && header[2] == 2) ?
        std::string((const char*)body.data(), size) : "Native telemetry refused Attraction Mode";
      break;
    }
    success = true; error.clear();
  } while (false);
    if (!success) close();
  return success;
  }
public:
  ~NativeAttractionConnection() { close(); }
  void close() { if (socket!=INVALID_SOCKET) closesocket(socket);socket=INVALID_SOCKET;port=0; }
  bool set(uint16_t endpoint,bool enabled,std::string& error) {
    if (!connectTo(endpoint,error)) return false;
    uint8_t flag=enabled?1:0;
    bool ok=command(30,&flag,1,error);
    // Keep the native connection for the entire suppression lease. Explicitly
    // restore normal mode before closing it; never depend on disconnect behavior.
    if (ok && !enabled) close();
    return ok;
  }
  bool keepAlive(std::string& error) {
    if (socket==INVALID_SOCKET) {error="Native suppression connection was lost";return false;}
    return command(0,nullptr,0,error);
  }
};
