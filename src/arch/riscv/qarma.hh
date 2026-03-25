#ifndef __ARCH_RISCV_QARMA_HH__
#define __ARCH_RISCV_QARMA_HH__

namespace gem5
{

namespace RiscvISA
{

static constexpr int MAX_LENGTH = 64;

using const_t = unsigned long long int;
using tweak_t = unsigned long long int;
using text_t = unsigned long long int;
using qkey_t = unsigned long long int;
using cell_t = unsigned char;

void text2cell(cell_t *cell, text_t is);
text_t cell2text(cell_t *cell);
text_t pseudo_reflect(text_t is, qkey_t tk);
text_t forward(text_t is, qkey_t tk, int r);
text_t backward(text_t is, qkey_t tk, int r);
cell_t LFSR(cell_t x);
cell_t LFSR_inv(cell_t x);
qkey_t forward_update_key(qkey_t T);
qkey_t backward_update_key(qkey_t T);
text_t qarma64_enc(text_t plaintext, tweak_t tweak, qkey_t w0, qkey_t k0,
                   int rounds);
text_t qarma64_dec(text_t plaintext, tweak_t tweak, qkey_t w0, qkey_t k0,
                   int rounds);

} // namespace RiscvISA
} // namespace gem5

#endif // __ARCH_RISCV_QARMA_HH__
