#include <vector>

#include "src/aliasing.h"

int main() {
    std::vector<float> v(64, 1.0F);
    e2e::Buffer buf{v.data(), v.size()};
    e2e::scale_slow(buf, v[0]);
    e2e::scale_fast(buf, 2.0F);
    return static_cast<int>(e2e::dot(v.data(), v.data(), v.size()));
}
