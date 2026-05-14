"""
FCO SV Automation Helper
========================
Import inside the existing SV session:

    import sys
    sys.path.insert(0, r'C:\\fco_automation')
    import sv_automation
    sv_automation.run_qdf_list(itp, sv, bs_wrap)

Edit qdf_list.json with the QDF/ULT pairs before running.
Coordinates with svos_automation.py via signal files in signals/
"""

import os
import sys
import ctypes
import time
import json
from pathlib import Path


def _alert(title: str, msg: str):
    """Shows a Windows alert popup and also prints to console."""
    print(f'\n!!! {title}: {msg} !!!\n', file=sys.stderr)
    ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR

BASE_DIR       = Path(__file__).parent
QDF_LIST_FILE  = BASE_DIR / 'qdf_list.json'
SIGNAL_DIR     = BASE_DIR / 'signals'

# config.json: search next to the script and in cwd as fallback
_CONFIG_SEARCH = [
    BASE_DIR / 'config.json',
    Path.cwd() / 'config.json',
]
CONFIG_FILE = next((p for p in _CONFIG_SEARCH if p.exists()), BASE_DIR / 'config.json')

def _load_fixed_params() -> dict:
    """Reads config.json and returns the fixed wrapper parameters."""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
        print(f'[sv_automation] config.json loaded from: {CONFIG_FILE}')
        return {k: v for k, v in cfg.items() if not k.startswith('_')}
    except FileNotFoundError:
        print(f'[!!] config.json not found (buscado en {[str(p) for p in _CONFIG_SEARCH]}). Using defaults.')
        return dict(proj='DMRUCC', stepping='a0', pwrgoodmethod='usb',
                    fused_unit=False, pwrgooddelay=30)
    except Exception as e:
        print(f'[!!] Error reading config.json: {e}. Using defaults.')
        return dict(proj='DMRUCC', stepping='a0', pwrgoodmethod='usb',
                    fused_unit=False, pwrgooddelay=30)

FIXED_PARAMS = _load_fixed_params()


def run_qdf_list(itp, sv, bs_wrap, qdf_list=None, signal_dir=None):
    """
    Runs the fuse overwrite for each QDF/ULT in the list.

    Args:
        itp       : itp object from the SV session
        sv        : sv object from the SV session
        bs_wrap   : bs_wrap module from the SV session
        qdf_list  : list of dicts [{'qdf':..., 'ult0':...}] or None (reads qdf_list.json)
        signal_dir: signal folder (default: signals/)
    """
    sig_dir = Path(signal_dir) if signal_dir else SIGNAL_DIR
    sig_dir.mkdir(parents=True, exist_ok=True)

    if qdf_list is None:
        with open(QDF_LIST_FILE) as f:
            qdf_list = json.load(f)

    print(f"\n{'='*60}")
    print(f"  FCO Automation: {len(qdf_list)} QDF(s) in queue")
    print(f"  Signal dir: {sig_dir}")
    print(f"{'='*60}\n")

    import users.mkcummin.mkc_fuse_utilities as fle  # noqa: F401
    import users.mkcummin.fle_bs_wrapper as bs_wrap  # noqa: F401

    # Clean up signals from previous runs to prevent SV from skipping the wait
    for item in qdf_list:
        qdf = item['qdf']
        for name in [f'{qdf}_sv_done.signal', f'{qdf}_svos_done.signal']:
            sig = sig_dir / name
            if sig.exists():
                sig.unlink()
                print(f'  [cleanup] Previous signal removed: {name}')

    for i, item in enumerate(qdf_list):
        qdf        = item['qdf']
        ult0       = item['ult0']
        soc        = item.get('soc', 'x4')
        extra_args = item.get('extra_args') or None
        fused_unit = item.get('fused_unit')  # None → usa el valor de config.json

        # New unified kwargs (backward compat: old items may still have extra_args/fused_unit)
        item_kwargs = dict(item.get('kwargs') or {})
        if not item_kwargs:
            if extra_args:
                item_kwargs['extra_args'] = extra_args
            if fused_unit is not None:
                item_kwargs['fused_unit'] = fused_unit
        fused_unit = item_kwargs.get('fused_unit')  # for display only

        print(f"\n[{i+1}/{len(qdf_list)}] QDF={qdf}  ULT={ult0}  SOC={soc}"
              + (f"  fused_unit={fused_unit}" if fused_unit is not None else "")
              + (f"  kwargs={item_kwargs}" if item_kwargs else ""))
        print('-' * 50)

        # Wait for SVOS to finish the previous QDF before continuing
        if i > 0:
            prev_qdf  = qdf_list[i - 1]['qdf']
            svos_done = sig_dir / f'{prev_qdf}_svos_done.signal'
            print(f"  Waiting for SVOS to finish {prev_qdf}...")
            _wait_for_file(svos_done)
            print(f"  SVOS ready. Continuing with {qdf}.")

        # Execute fuse overwrite
        try:
            _run_sv_fuse(itp, sv, bs_wrap, qdf, ult0, soc, **item_kwargs)
        except Exception as e:
            print(f"\n  [!!] Error in fuse overwrite for {qdf}: {e}")
            if i < len(qdf_list) - 1:
                resp = input(f"  Continue with next QDF? (y/n): ").strip().lower()
                if resp not in ('s', 'y', 'yes'):
                    print("  Process aborted by the user.")
                    return
                # Write sv_done signal anyway so SVOS does not remain blocked
                sv_done = sig_dir / f'{qdf}_sv_done.signal'
                sv_done.write_text('error\n')
                print(f"  Error signal written: {sv_done.name}")
                continue
            else:
                return

        # Signal svos_automation that it can proceed
        sv_done = sig_dir / f'{qdf}_sv_done.signal'
        sv_done.write_text('done\n')
        print(f"  Signal written: {sv_done.name}")
        print(f"  SVOS can proceed with {qdf}")

    # Wait for SVOS to finish the last QDF
    last_qdf   = qdf_list[-1]['qdf']
    last_done  = sig_dir / f'{last_qdf}_svos_done.signal'
    print(f"\nWaiting for SVOS to finish the last QDF ({last_qdf})...")
    _wait_for_file(last_done)

    print("\n" + "="*60)
    print("  MAIN LOOP COMPLETED")
    print("="*60)

    # ---- PHASE 2: Retry if SVOS requests it (BIOS/MOUNTSV timeout) ----
    retry_signal = sig_dir / 'retry_needed.signal'
    if _wait_for_file_timeout(retry_signal, timeout=60):
        retry_json = sig_dir / 'retry_needed.json'
        retry_items = json.load(open(retry_json, encoding='utf-8'))
        print(f"\n[RETRY] {len(retry_items)} QDF(s) para retry: "
              f"{[r['qdf'] for r in retry_items]}")

        # Power cycle
        try:
            from diamondrapids.toolext.bootscript.toolbox.power_control.power_controller \
                import PowerController
            pc = PowerController(power_controller_name='usb')
            print("  [RETRY] Power OFF...")
            pc.power_off()
            print("  [RETRY] Waiting 5 min (300s)...")
            time.sleep(300)
            print("  [RETRY] Power ON...")
            pc.power_on()
            print("  [RETRY] Waiting 60s for the system to boot...")
            time.sleep(60)
        except Exception as e:
            print(f"  [!!] Error during power cycle: {e}. Continuing without power cycle...")

        # Re-overwrite for each failed QDF
        for item in retry_items:
            qdf        = item['qdf']
            ult0_r     = item['ult0']
            soc_r      = item.get('soc', 'x4')
            retry_kwargs = dict(item.get('kwargs') or {})
            if not retry_kwargs:
                extra_r = item.get('extra_args')
                fused_r = item.get('fused_unit')
                if extra_r:
                    retry_kwargs['extra_args'] = extra_r
                if fused_r is not None:
                    retry_kwargs['fused_unit'] = fused_r
            print(f"\n  [RETRY] Fuse overwrite for {qdf}...")
            try:
                _run_sv_fuse(itp, sv, bs_wrap, qdf, ult0_r, soc_r, **retry_kwargs)
                (sig_dir / f'{qdf}_retry_sv_done.signal').write_text('done\n')
                print(f"  [RETRY] retry_sv_done signal written for {qdf}")
            except Exception as e:
                print(f"  [!!] Error in retry overwrite for {qdf}: {e}")
                (sig_dir / f'{qdf}_retry_sv_done.signal').write_text('error\n')

        # Notify SVOS that the power cycle and overwrites are ready
        (sig_dir / 'retry_ready.signal').write_text('ready\n')
        print("  [RETRY] retry_ready.signal written. Waiting for SVOS to finish retries...")

        # Wait for SVOS to finish all retries
        for item in retry_items:
            qdf = item['qdf']
            _wait_for_file(sig_dir / f'{qdf}_retry_svos_done.signal')
            print(f"  [RETRY] SVOS finished retry for {qdf}")

        print("\n" + "="*60)
        print("  ALL RETRIES COMPLETED")
        print("="*60)
    else:
        print("\n  [INFO] No QDFs for retry.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_sv_fuse(itp, sv, bs_wrap, qdf, ult0, soc='x4', **kwargs):
    print("  forcereconfig / unlock ...")
    itp.forcereconfig()
    itp.unlock()
    itp.forcereconfig()
    sv.refresh()

    # Build full parameters; kwargs overrides FIXED_PARAMS where keys overlap
    all_params = dict(qdf=qdf, ult0=ult0, soc=soc, **FIXED_PARAMS)
    all_params.update(kwargs)  # fused_unit, pwrgoodmethod, extra_args, etc.

    params_str = ', '.join(f"{k}={v!r}" for k, v in all_params.items())
    print(f"\n  Command to run:")
    print(f"  bs_wrap.main({params_str})")
    print()

    bs_wrap.main(**all_params)
    print(f"  [OK] bs_wrap.main completed for {qdf}")


def _wait_for_file(filepath, poll=10):
    """Waits indefinitely until the file exists."""
    while not Path(filepath).exists():
        time.sleep(poll)


def _wait_for_file_timeout(filepath, poll=5, timeout=60):
    """Waits up to timeout seconds. Returns True if it arrived, False if it expired."""
    deadline = time.time() + timeout
    while not Path(filepath).exists():
        if time.time() > deadline:
            return False
        time.sleep(poll)
    return True
