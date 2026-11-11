"""The context words (docs/specs/S0003, ARTIFACT.md part 3): fixed words, then the variable part
in the order memories, tables, globals, function imports, libcalls, signature ids, function
records. Shared by every JIT architecture.

A table element and a `funcref` value are one word: 0 (null) or the address of a function record
`{code, type id, context}` (3 words); an `externref` is a host handle (0 = null). A module's records
(one per function it can take a reference to) and the canonical type ids its `call_indirect`s
expect (one word per signature) live in its own context, written by the runtime."""

from dataclasses import dataclass, field

SAVED_SP, TRAP_CODE, INTERRUPT, FUEL, EPOCH_DEADLINE = 0, 1, 2, 3, 4
VSTACK_LIMIT, NSTACK_LIMIT, HOST_ERROR, HOST_BRIDGE = 5, 6, 7, 8
TAIL_SWITCH = 9  # address of the cross-context tail-call trampoline (D30)
# exceptions (S0012): addresses of the store's pending-exception word and of its arena cursor and
# limit (one arena per store: every context of the store allocates from it), and the service that
# gives the arena a new chunk, called through the bridge only when the current one is full
EXCEPTION, EXN_REFILL, ARENA_CURSOR, ARENA_LIMIT = 10, 11, 12, 13
# the address of the store's exit record {sp, pc, frame}: written by the bridge at every host
# call, sp cleared after it, saved and restored by every entry (S0013 3a)
EXIT_RECORD = 14
FIRST_VAR = 16  # word 15 is reserved, zero

# Status values returned by the entry, the bridge and every function (0 = success).
STATUS_INTERRUPT = 12
STATUS_HOST_ERROR = 13
STATUS_EXCEPTION = 14  # inside code: an exception is in flight (S0012)

# Libcalls (S0003): runtime services, one context word each after the imports, in this order.
MEMORY_GROW, TABLE_GROW, TABLE_INIT, ELEM_DROP, MEMORY_INIT, DATA_DROP = range(6)
LIBCALLS = 6
RECORD = 3  # words of a function record: code address, canonical type id, context address


@dataclass(frozen=True)
class Layout:
    """Word indices of an instance's context, from the module's counts."""

    memories: int = 0
    tables: int = 0  # 2 words each
    globals: int = 0  # 1 word each (2 for one in `wide`)
    imports: int = 0
    sigs: int = 0  # canonical type ids that call_indirect compares against, 1 word each
    records: int = 0  # function records, RECORD words each
    # defined v128 globals (2 words each, S0005); not in the repr, so the D35 lists of modules
    # without v128 stay as they were (the word numbers in the code show it anyway)
    wide: tuple[int, ...] = field(default=(), repr=False)
    tags: int = field(default=0, repr=False)  # tag identity words after the records (S0012)

    def memory(self, i: int) -> int:
        """`base`; `size_bytes` is the next word."""
        return FIRST_VAR + 2 * i

    def table(self, i: int) -> int:
        """`base` (address of the element words); `size` (elements) is the next word."""
        return FIRST_VAR + 2 * self.memories + 2 * i

    def global_(self, i: int) -> int:
        """Global i (imports first): its value, or for an imported one the address of it."""
        return FIRST_VAR + 2 * self.memories + 2 * self.tables + i + sum(w < i for w in self.wide)

    def import_(self, i: int) -> int:
        return FIRST_VAR + 2 * self.memories + 2 * self.tables + self.globals + len(self.wide) + i

    def libcall(self, j: int) -> int:
        return self.import_(self.imports) + j

    def sig(self, k: int) -> int:
        return self.libcall(LIBCALLS) + k

    def record(self, r: int) -> int:
        return self.sig(self.sigs) + RECORD * r

    def tag(self, t: int) -> int:
        return self.record(self.records) + t

    @property
    def words(self) -> int:
        # at least memory 0's two words: the entry loads them whether or not the module has a memory
        return max(self.tag(self.tags), FIRST_VAR + 2)
