/* Prepare Matrices: the skinning matrices of a cloth's bones, bit-exact against the game's routine (Havok's object-space
   skinning operator). What it computes: cloth-spec/matrices/SPEC.md. This header is the interface; a candidate
   cloth_matrices.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_MATRICES_H
#define CLOTH_MATRICES_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* One call of the routine. Every pointer is a host pointer to bytes laid out as the game keeps them (little-endian
   binary32 floats, 16-byte columns). The hook has checked that every range below is mapped and that `out` meets none of
   the ranges read; the kernel never has to decline. */
typedef struct ClothMatricesArgs {
    const uint8_t *model;   /* M: one 4x4 matrix, 64 bytes: four columns M0..M3, each four floats (lanes 0..3) */
    const uint8_t *bones;   /* the bone matrices, 64 bytes each (same layout); bone k at bones + 64 k */
    const uint8_t *index;   /* one unsigned 16-bit bone index per entry, 2 bytes each */
    const uint8_t *in;      /* one 64-byte matrix per entry (same layout) */
    uint8_t *out;           /* one 64-byte matrix per entry, written by the kernel (same layout) */
    uint32_t count_word;    /* the routine's count argument, low 32 bits as given: SPEC.md 1 ("the count") */
    int fz;                 /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
    uint32_t fpsr_in;       /* the float status flags already set when the routine is called (bits: CLOTH_MX_*) */
    uint8_t *left;          /* out: CLOTH_MX_LEFT columns of 16 bytes (four floats, lane 0 first): the values left for
                               the caller, left[k] at left + 16 k (SPEC.md 6); any alignment */
} ClothMatricesArgs;

#define CLOTH_MX_LEFT 7

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_MX_IOC = 0x01u,   /* invalid operation */
    CLOTH_MX_DZC = 0x02u,   /* divide by zero (never raised here) */
    CLOTH_MX_OFC = 0x04u,   /* overflow */
    CLOTH_MX_UFC = 0x08u,   /* underflow */
    CLOTH_MX_IXC = 0x10u,   /* inexact (always already set at a call: need not be reported) */
    CLOTH_MX_IDC = 0x80u    /* input denormal (flush-to-zero mode only) */
};

/* Computes every entry, writes `out` and the seven columns at `left`, and returns the status bits the routine raises.
   The hook ORs the return value into the status register: the return value R is correct when
   (fpsr_in | R) == (fpsr_in | raised), so bits already set in fpsr_in, and IXC, may be left out. Writes nothing but
   out[0 .. 64 * count) and left[0 .. 16 * CLOTH_MX_LEFT). Called only with a count of at least one entry. */
uint32_t cloth_matrices_81cdff40(const ClothMatricesArgs *a);

#ifdef __cplusplus
}
#endif
#endif
