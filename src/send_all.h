#pragma once
#include <cstddef>
#include <cstdint>
#include <algorithm>
#include <climits>

// A successful stream write may accept only part of a frame.
template<class Send>
bool SendAll(const uint8_t* data, size_t size, Send send) {
  size_t offset = 0;
  while (offset < size) {
    const int chunk = static_cast<int>(std::min(size - offset, size_t(INT_MAX)));
    const int sent = send(data + offset, chunk);
    if (sent <= 0 || sent > chunk) return false;
    offset += size_t(sent);
  }
  return true;
}
