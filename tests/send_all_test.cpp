#include "../src/send_all.h"
#include <cassert>
#include <vector>

int main() {
  std::vector<uint8_t> input(65545), received;
  for (size_t i = 0; i < input.size(); ++i) input[i] = uint8_t(i);
  int calls = 0;
  assert(SendAll(input.data(), input.size(), [&](const uint8_t* p, int n) {
    ++calls;
    int accepted = std::min(n, 137);
    received.insert(received.end(), p, p + accepted);
    return accepted;
  }));
  assert(calls > 1 && received == input);
  calls = 0;
  assert(!SendAll(input.data(), input.size(), [&](const uint8_t*, int) {
    return ++calls == 1 ? 20 : -1;
  }));
  assert(calls == 2);
  assert(!SendAll(input.data(), input.size(), [](const uint8_t*, int) { return 0; }));
  assert(SendAll(input.data(), 0, [](const uint8_t*, int) { assert(false); return -1; }));
}
