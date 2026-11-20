"""heap.c: a segment of several pieces of direct memory when no single piece is free that is large enough.

The direct memory the title shares (the game's pages, the driver's buffers, the heap) ends up in many small pieces: on
2026-11-18 1254 MiB were free in 3616 pieces, the largest 9 MiB, and a 16 MiB malloc (a segment of 16 MiB) waited for
good. A segment is one range of the heap's addresses; what is under it need not be one piece. Each piece is a segment
entry of its own, back to back, which heap_unmap_locked already takes as tiling a span. The slot search also learns that
two entries may touch (no gap between the pieces of one segment)."""
import pathlib, sys

p = pathlib.Path(sys.argv[1]) / 'src/heap.c'
s = p.read_text()

def sub(old, new):
    global s
    if s.count(old) != 1:
        sys.exit('heap_pieces: anchor x%d: %r' % (s.count(old), old[:70]))
    s = s.replace(old, new)

sub('''   for (; slot < segment_count; slot++) {
      if (segments[slot].offset - offset >= bytes + HEAP_GAP)
         break;
      offset = segments[slot].offset + segments[slot].bytes + HEAP_GAP;
   }''', '''   for (; slot < segment_count; slot++) {
      /* (entries may touch: the pieces of one segment, heap_map_pieces) */
      if (segments[slot].offset >= offset && segments[slot].offset - offset >= bytes + HEAP_GAP)
         break;
      const size_t next = segments[slot].offset + segments[slot].bytes + HEAP_GAP;
      if (next > offset)
         offset = next;
   }''')

sub('''   int64_t start = -1;
   if (ps5p_allocate_direct(bytes, &start) != 0)
      return (void *)-1;
   void *const at = (void *)(range_base + offset);''', '''   int64_t start = -1;
   if (ps5p_allocate_direct(bytes, &start) != 0)
      return heap_map_pieces(slot, offset, bytes);
   void *const at = (void *)(range_base + offset);''')

sub('''static void *
heap_map_locked(size_t bytes)
{''', '''#define HEAP_PIECES 256

/* (Longshot, ps5/sdk-patches/heap_pieces.py) No piece of direct memory large enough: the segment at offset made of
 * several, the largest free first, each mapped after the one before and each its own entry from slot. All or none. */
static void *
heap_map_pieces(unsigned slot, size_t offset, size_t bytes)
{
   static struct heap_segment got[HEAP_PIECES];
   unsigned n = 0;
   size_t done = 0;
   while (done < bytes && n < HEAP_PIECES && segment_count + n < HEAP_MAX_SEGMENTS) {
      size_t piece = bytes - done;
      int64_t at_phys = 0;
      size_t largest = 0;
      if (sceKernelAvailableDirectMemorySize(0, sceKernelGetDirectMemorySize(), HEAP_UNIT, &at_phys, &largest) == 0 &&
          largest < piece)
         piece = largest & ~(size_t)(HEAP_UNIT - 1);
      int64_t start = -1;
      while (piece >= HEAP_UNIT && ps5p_allocate_direct(piece, &start) != 0)
         piece = piece / 2 & ~(size_t)(HEAP_UNIT - 1);
      if (piece < HEAP_UNIT)
         break;
      void *const at = (void *)(range_base + offset + done);
      void *mapped = at;
      if (sceKernelMapDirectMemory(&mapped, piece, HEAP_PROTECTION, PS5_KERNEL_MAP_FIXED, start, HEAP_UNIT) != 0 ||
          mapped != at) {
         sceKernelReleaseDirectMemory(start, piece);
         break;
      }
      got[n++] = (struct heap_segment){.offset = offset + done, .bytes = piece, .direct_start = start};
      done += piece;
   }
   if (done < bytes) {
      for (unsigned i = 0; i < n; i++) {
         void *at = (void *)(range_base + got[i].offset);
         sceKernelMunmap(at, got[i].bytes);
         sceKernelReserveVirtualRange(&at, got[i].bytes, PS5_KERNEL_MAP_FIXED, PS5P_PAGE);
         sceKernelReleaseDirectMemory(got[i].direct_start, got[i].bytes);
      }
      return (void *)-1;
   }
   memmove(&segments[slot + n], &segments[slot], (segment_count - slot) * sizeof(segments[0]));
   memcpy(&segments[slot], got, n * sizeof(got[0]));
   segment_count += n;
   mapped_bytes += bytes;
   if (mapped_bytes > peak_bytes)
      peak_bytes = mapped_bytes;
   return (void *)(range_base + offset);
}

static void *
heap_map_locked(size_t bytes)
{''')
if 'sceKernelAvailableDirectMemorySize' not in s.split('heap_map_pieces')[0]:
    s = s.replace('#define HEAP_PIECES 256', '''int32_t sceKernelAvailableDirectMemorySize(int64_t start, int64_t end, size_t alignment, int64_t *found, size_t *size);
#define HEAP_PIECES 256''', 1)
p.write_text(s)
print('heap_pieces: ok')
