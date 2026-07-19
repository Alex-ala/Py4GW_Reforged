# Multibox Shared Memory -> C++ Writer Migration: Incident & Resolution

Status: **RESOLVED / working.** The C++ writer now produces a byte-exact mirror of
the Python `AllAccounts` layout, Python maps and reads it every frame, and the
multibox shared memory functions correctly. This document records what broke, why
it took so long to find, and the fix, so the mistake is not repeated.

Companion design doc: `docs/multibox_shmem_cpp_writer_plan.md`.

---

## 1. What was migrated

The **writer** of the multi-account ("multibox") shared-memory buffer
(`Py4GW_Shared_Mem`, the `GLOBAL_CACHE.ShMem` surface) moved from Python into a new
C++ module in `Py4GW_Reforged_Native`. Python became a pure reader. Byte layout is
unchanged; **Python remains the source of truth and the C++ structs mirror it.**

Three changes: (1) C++ owns the push; (2) slots keyed on the stable C++ account
email anchor; (3) full push every frame, no throttle. Plus: zero the payload during
a map load. Coordination regions (`Inbox`, `HeroAIOptions`, `Intents`) stay
Python-written.

Files: NEW `include/GW/multibox/manager.h` + `src/GW/multibox/manager.cpp`;
`src/Py4GW.cpp` create/update/destroy wiring (`Update()` at the top of `DrawLoop`);
Python `SharedMemory.py` (attach-only, coordination-only callback) and
`AllAccounts.py` (`GetSlotByEmail` no auto-submit).

---

## 2. The bug (root cause)

**One wrong constant.** The C++ mirror hardcoded

```
kMaxAttributes = 43   // WRONG
```

but the Python `AttributesStruct` uses `len(Attribute)`, which is **46**
(the enum includes `Unknown1/2/3` and `None_`, which a bad grep had missed).

Consequences, exactly:
- `AttributesStruct`: C++ `43 x 12 = 516` bytes vs Python `46 x 12 = 552` -> 36 bytes short.
- That deficit lives inside `AgentDataStruct`, so every `AccountStruct` was 36 bytes short.
- `AllAccounts` = 64 slots, so the C++ buffer was created **~2 KB smaller** than
  Python's `ctypes.sizeof(AllAccounts) = 940096`.
- Python `AllAccounts.from_buffer(self.shm.buf)` cannot map its struct onto a
  too-small buffer, so it raised **`ValueError: Buffer size too small` every frame**,
  from every reader (`GetAllAccounts`, `GetAllAccountData`, `GetNextMessage`, HeroAI
  party cache, `SharedMem Monitor`, `Messaging`, ...).

That is what presented as **"flicker"**: widgets alternating between last-cached
values and a failed read. The data was never actually readable; nothing was being
torn or raced. The confirming evidence was a single line in `runtime_errors.txt`:

```
ValueError: Buffer size too small (937984 instead of at least 940096 bytes)
```

**Why the guardrail did not catch it.** The plan called the byte-identical layout
"the linchpin" and said to assert against the LIVE `ctypes.sizeof`. That was not
done: the C++ `static_assert`s shipped with **hand-computed** sizes that matched
neither Python nor the real C++ output, so the trip-wire meant to catch exactly this
was calibrated to fiction and waved a broken layout through to runtime.

---

## 3. Why it took so long (rejected hypotheses)

Before the error log existed, the "flicker" was chased through four plausible but
**wrong** causes, each fixed cleanly with no effect, because all of them sat *above*
a buffer Python could never map:

1. Writer thread/ordering (moved `Update()` to the top of `DrawLoop`).
2. `LastUpdated` clock mismatch (switched to the frame-coherent `PY4GW::System::GetTickCount64`).
3. Zero-on-gate too broad (narrowed to a real map load only).
4. Whole-struct wipe-then-refill each frame (switched to in-place overwrites).

A GIL / torn-read theory was also floated and was **wrong** — no read ever completed
to be torn. The lesson: when several clean fixes do not move the needle, stop and get
an error log; it named the real cause in one line.

---

## 4. The fix

1. `kMaxAttributes` **43 -> 46** in `manager.h`.
2. **Recalibrated every `static_assert` to the authoritative live Python
   `ctypes.sizeof`**, not hand math. Key values now enforced:
   - `AttributesStruct == 552`, `AgentDataStruct == 5772`, `AccountStruct == 13639`,
     `AllAccounts == 940096` (the exact number from the runtime `ValueError`).
   The build now physically cannot pass unless C++ is byte-exact with Python.
3. The four refinements from section 3 are **correct and retained** — they are not
   what broke it, but they are the right behavior for a C++ writer (frame-coherent
   liveness clock; writer first in `DrawLoop`; map-load-only zeroing; in-place
   overwrites so a cross-process reader never sees a value pass through zero).

Result: C++ `sizeof(AllAccounts) == 940096 == Python`, the buffer maps, readers
succeed, the shared memory works.

`shmem_layout_dump.py` (repo root) dumps the authoritative Python per-struct sizes
and field offsets; keep it as the calibration tool if the layout ever changes again.

---

## 5. Lessons

- **A byte-identical contract must be asserted against the live `ctypes.sizeof`,
  never a hand-computed number.** The one guardrail that would have caught this in
  seconds was miscalibrated by hand and gave false confidence. This was the whole
  failure.
- **Derived constants are landmines.** `len(Attribute)` was read via a fragile grep
  (43) instead of the actual enum (46). Read counts literally from the source.
- **When clean fixes do nothing, get an error log before theorizing further.** The
  log pointed at the real cause immediately; four rounds of reasoning did not.
- The migration approach was sound the whole time; it was gated on a single wrong
  constant plus an uncalibrated assert.
