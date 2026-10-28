#include <cstdio>
#include <vector>

#include "examples/kernels.h"

int main() {
    std::vector<double> v{2, 1, 2, 3, 4};
    examples::scale_down(v, v[0]);
    examples::apply_gain_all(v.data(), v.size());
    std::printf("%f\n", examples::sum(v.data(), v.size()));
    return 0;
}
