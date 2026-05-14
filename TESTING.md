# FCO AutoTool Testing — CentOS Boot Automation Flow

## Test Scenario 1: Mode 2 (Fused Unit) — CentOS Boot

**Setup:**
- Mode 2 selected (fused unit)
- Content selected: SVOS boot check + CentOS boot
- sv_automation running in idle loop

**Expected Flow:**
```
1. Tool 1 (FCO_AutoTool):
   └─ Boots SVOS successfully
   └─ Runs SVOS boot check (runs svosinfo)
   └─ User selects CentOS boot
   └─ Writes: {QDF}_centos_power_cycle.signal = "requested"
   └─ Waits for: {QDF}_centos_power_cycled.signal

2. pysv (idle loop):
   └─ Detects {QDF}_centos_power_cycle.signal
   └─ Executes: power OFF → 5s wait → power ON → 15s boot wait
   └─ Writes: {QDF}_centos_power_cycled.signal = "done"

3. Tool 1 (FCO_AutoTool) — continued:
   └─ Detects {QDF}_centos_power_cycled.signal
   └─ Proceeds with: boot_centos() (BIOS → UEFI → BootCentosDMR.efi)
   └─ CentOS boots successfully
   └─ Writes: {QDF}_svos_done.signal = "done"

4. pysv (idle loop):
   └─ Detects {QDF}_svos_done.signal = "done"
   └─ Knows QDF completed successfully
   └─ Continues monitoring for next CentOS request (other QDF or retry_needed)
```

**Validation Checklist:**
- [x] Signal files created with correct names: `{QDF}_centos_power_cycle.signal`
- [x] pysv receives signal and power cycles (5s OFF, 15s boot)
- [x] pysv writes completion signal: `{QDF}_centos_power_cycled.signal`
- [x] FCO_AutoTool detects completion and continues CentOS boot
- [x] svos_done written = "done" (indicates success to pysv)

---

## Test Scenario 2: Mode 1 (Non-Fused Unit) — CentOS Boot

**Setup:**
- Mode 1 selected (non-fused, single QDF)
- Content selected: Rocket + CentOS boot
- sv_automation running in idle loop

**Expected Flow:**
```
1. Tool 1 (FCO_AutoTool):
   └─ Waits for {QDF}_sv_done.signal from main loop SV overwrite
   └─ Boots SVOS successfully
   └─ Runs Rocket test
   └─ User selects CentOS boot
   └─ Writes: {QDF}_centos_wrapper.signal = JSON{"qdf":"...", "ult0":"...", "soc":"...", "kwargs":{}}
   └─ Waits for: {QDF}_centos_wrapper_done.signal

2. pysv (idle loop):
   └─ Detects {QDF}_centos_wrapper.signal
   └─ Parses JSON: qdf, ult0, soc, kwargs
   └─ Executes: bs_wrap.main(qdf=..., ult0=..., soc=..., **kwargs, **FIXED_PARAMS)
   └─ Writes: {QDF}_centos_wrapper_done.signal = "done"

3. Tool 1 (FCO_AutoTool) — continued:
   └─ Detects {QDF}_centos_wrapper_done.signal = "done"
   └─ Proceeds with: boot_centos() (BIOS → UEFI → BootCentosDMR.efi)
   └─ CentOS boots successfully
   └─ Writes: {QDF}_svos_done.signal = "done"

4. pysv (idle loop):
   └─ Detects {QDF}_svos_done.signal = "done"
   └─ Continues monitoring (no more QDFs in mode 1, waits for retry_needed)
```

**Validation Checklist:**
- [x] Signal files: `{QDF}_centos_wrapper.signal` contains valid JSON
- [x] JSON includes: qdf, ult0, soc, kwargs keys
- [x] pysv parses JSON correctly
- [x] bs_wrap.main() called with all params merged (FIXED_PARAMS + JSON params + overrides)
- [x] pysv writes: `{QDF}_centos_wrapper_done.signal` = "done"
- [x] FCO_AutoTool detects and continues CentOS boot
- [x] svos_done written = "done"

---

## Test Scenario 3: Mode 3 (Fused + Overwrite Others) — Multiple QDFs

**Setup:**
- Mode 3: fused QDF + 2 QDFs to overwrite
- Content for each: Some SVOS tests + CentOS boot
- sv_automation running in idle loop

**Expected Flow:**
```
QDF 1 (Fused):
  1. SV already fused (skipped)
  2. SVOS boots
  3. SVOS tests run (Rocket, MLC)
  4. CentOS boot → Mode 2 power cycle → boots successfully
  5. Writes: {QDF1}_svos_done.signal = "done"
  6. pysv sees "done" → ready for next QDF

QDF 2 (Overwrite):
  1. pysv (main loop — NOT idle loop) waits for {QDF1}_svos_done.signal ✓
  2. SV fuses QDF2 (forcereconfig → unlock → bs_wrap.main)
  3. Writes: {QDF2}_sv_done.signal
  4. FCO_AutoTool boots SVOS
  5. SVOS tests run
  6. CentOS boot → Mode 1/3/4 wrapper → boots successfully
  7. Writes: {QDF2}_svos_done.signal = "done"
  8. pysv (main loop) continues to QDF3

QDF 3 (Overwrite):
  [same as QDF2 flow]
```

**Validation Checklist:**
- [x] QDF1 (fused) uses Mode 2 CentOS flow (power cycle)
- [x] QDF2/3 (overwrites) use Mode 1/3/4 CentOS flow (wrapper)
- [x] pysv main loop waits for {QDF_N}_svos_done.signal before starting QDF_N+1
- [x] All svos_done signals written with correct content ("done" or "error")
- [x] Idle loop only activates after main loop completes

---

## Test Scenario 4: CentOS Boot Failure — Fallback to Retry

**Setup:**
- Mode 1, single QDF
- CentOS boot fails (e.g., BootCentosDMR.efi not found)

**Expected Flow:**
```
1. FCO_AutoTool attempts CentOS boot → Exception raised
2. Exception caught in run_centos_boot()
3. Returns: 'FAIL'
4. Propagates back to _run_main_loop()
5. centos_result = 'FAIL'
6. results['centos_boot'] = 'FAIL'
7. overall != 'PASS' → svos_done written = "error"
8. all_results appended with overall = 'FAIL'
9. No retry_needed entry added (CentOS failure ≠ BIOS/MOUNTSV timeout)
10. pysv (idle loop) sees svos_done = "error" → knows QDF failed
```

**Validation Checklist:**
- [x] Exception handling catches CentOS boot errors
- [x] Function returns 'FAIL', not exception
- [x] overall result = 'FAIL'
- [x] svos_done written = "error" (not "done")
- [x] Logging captures exception details
- [x] pysv idle loop can continue monitoring (no hang)

---

## Test Scenario 5: Mode 2 CentOS Power Cycle Failure

**Setup:**
- Mode 2, CentOS boot selected
- Power cycle command fails (e.g., PowerController error)

**Expected Flow:**
```
1. FCO_AutoTool writes: {QDF}_centos_power_cycle.signal
2. pysv (idle loop) detects signal
3. pysv tries PowerController → Exception raised
4. Exception caught in try/except
5. pysv writes: {QDF}_centos_power_cycled.signal = "error"
6. pysv loops back to idle monitoring
7. FCO_AutoTool waits for centos_power_cycled.signal
8. Detects content = "error" → ??? Currently no explicit check!
```

⚠️ **ISSUE DETECTED:** 
- `run_centos_boot()` Mode 2 doesn't check if `centos_power_cycled.signal` = "error"
- Currently only checks that signal exists, reads content but doesn't validate

**FIX NEEDED:** Add validation for power cycle error

---

## Test Scenario 6: Mode 1/3/4 Wrapper Execution Failure

**Setup:**
- Mode 1, CentOS wrapper selected
- bs_wrap.main() fails (e.g., invalid params)

**Expected Flow:**
```
1. FCO_AutoTool writes: {QDF}_centos_wrapper.signal (JSON params)
2. pysv (idle loop) detects signal
3. pysv parses JSON → extracts params
4. pysv calls bs_wrap.main(**params) → Exception raised
5. Exception caught in try/except
6. pysv writes: {QDF}_centos_wrapper_done.signal = "error"
7. FCO_AutoTool waits for centos_wrapper_done.signal
8. Detects content = "error" ✓ Correctly handled
9. Returns 'FAIL'
```

**Validation Checklist:**
- [x] pysv catches wrapper errors and writes "error" signal
- [x] FCO_AutoTool checks signal content and returns FAIL
- [x] No hang or deadlock

---

## Issues Found & Fixes Required

### Issue 1: Mode 2 CentOS Power Cycle Error Not Validated ⚠️

**Location:** `FCO_AutoTool.py` line ~1455 in `run_centos_boot()`

**Current Code:**
```python
_wait_for_file(power_cycled_signal)
_status('Power cycle completed by pysv.', 'ok')
```

**Problem:** Doesn't check if power cycle actually succeeded (signal content = "error")

**Fix:**
```python
_wait_for_file(power_cycled_signal)
done_content = power_cycled_signal.read_text().strip()
if done_content == 'error':
    _status('pysv reported error during power cycle.', 'fail')
    return 'FAIL'
_status('Power cycle completed by pysv.', 'ok')
```

---

### Issue 2: Wrapper Signal Cleanup for Mode 1/3/4 ⚠️

**Location:** `FCO_AutoTool.py` in `run_centos_boot()` Mode 1/3/4 section

**Current Code:**
```python
for name in [f'{qdf}_centos_wrapper.signal', f'{qdf}_centos_wrapper_done.signal']:
    sig = SIGNAL_DIR / name
    if sig.exists():
        sig.unlink()
```

**Problem:** Cleans signals AFTER mode check (if Mode 2, these cleanup doesn't run)
Also, these signals never get cleaned at loop start like Mode 2 signals do

**Consideration:** Is this OK or should we clean wrapper signals at the start of QDF loop?

---

## Summary

✅ **Strengths:**
- Clean signal-based coordination
- Idle loop architecture is sound
- Error handling for most cases
- Main loop properly waits for svos_done

⚠️ **Issues Found:**
1. Mode 2 power cycle error not validated — needs fix
2. Wrapper signals cleanup logic could be more consistent

🔧 **Recommendations:**
1. Add error validation for power cycle (Issue 1)
2. Consider cleaning wrapper signals at loop start (Issue 2 — less critical)
3. Add logging for signal creation/detection (already good)
4. Consider timeout on signal waits (currently infinite `_wait_for_file()`)

