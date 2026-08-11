"""
FCO AutoTool - Serial Automation
=================================
Run this script in a separate Python window while sv_automation.py
runs inside your SV session.

Uso:
    python FCO_AutoTool.py

Requisitos:
    pip install pyserial

Configuration:
    - Edit qdf_list.json with the QDF/ULT pairs
    - Adjust OS_FCO_PROMPTS if the os_fco.sh prompts are different
    - The script prompts for COM port and week number at startup
"""

import os
import sys
import re
import json
import time
import ctypes
import serial
import logging
import argparse
import datetime
import contextlib
import threading
import queue as pyqueue
from pathlib import Path

# Windows keypress detection during timeouts
if sys.platform == 'win32':
    import msvcrt


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class FCOStepError(Exception):
    """Error in a specific workflow step. Carries a user-friendly description."""
    pass

class MountsvTimeoutError(FCOStepError):
    """mountsv did not respond within the expected time — retryable via power cycle."""
    pass

class BiosTimeoutError(FCOStepError):
    """BIOS screen did not appear within the expected time — retryable via power cycle."""
    pass


@contextlib.contextmanager
def _guard(step_desc: str):
    """
    Wraps a code block and converts TimeoutError / SerialException
    into FCOStepError with a descriptive message of the failed step.
    """
    try:
        yield
    except TimeoutError:
        raise FCOStepError(f'TIMEOUT waiting for: {step_desc}')
    except serial.SerialException as e:
        raise FCOStepError(f'Serial error in "{step_desc}": {e}')


def _alert_popup(title: str, msg: str):
    """Non-blocking warning popup + prints to console."""
    import threading
    print(f'\n[!!] {title}\n     {msg}\n', flush=True)
    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
        except Exception:
            pass
    threading.Thread(target=_show, daemon=True).start()


def _alert_popup_async(title: str, msg: str):
    """Non-blocking informational popup (separate thread). Execution continues immediately."""
    import threading
    print(f'\n[!] {title}: {msg}', flush=True)
    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
        except Exception:
            pass
    threading.Thread(target=_show, daemon=True).start()

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

BASE_DIR      = Path(__file__).parent
SIGNAL_DIR    = BASE_DIR / 'signals'
LOG_DIR       = BASE_DIR / 'logs'
REPORTS_DIR   = LOG_DIR / 'reports'
SERIAL_SEGMENT_LOG_DIR = LOG_DIR / 'Serial Logs'
QDF_LIST_FILE    = BASE_DIR / 'qdf_list.json'
LAST_CONFIG_FILE = BASE_DIR / 'last_config.json'
LAST_BOOT_SVOS_CONFIG_FILE = BASE_DIR / 'last_boot_svos_config.json'
LAST_BOOT_CENTOS_CONFIG_FILE = BASE_DIR / 'last_boot_centos_config.json'
LAST_UPDATE_CONFIG_FILE = BASE_DIR / 'last_update_config.json'
LAST_EFI_TIMING_CONFIG_FILE = BASE_DIR / 'last_efi_timing_config.json'
TIMEOUTS_CONFIG_FILE = BASE_DIR / 'timeouts_config.json'
WRAPPER_CONFIG_FILE = BASE_DIR / 'config.json'

# GitHub repo for auto-update
GITHUB_REPO_URL = 'https://github.com/egmonter/FCO_AutoTool.git'

BAUDRATE      = 115200

# BIOS screen identifier text — any of these is accepted
BIOS_BANNERS   = [b'OAKSTREAM', b'EDKII', b'Intel Corporation',
                  b'Boot Manager', b'UEFI', b'EDK II']
BIOS_BOOT_MGR  = 'Boot Manager'        # option in the BIOS menu
BIOS_INT_SHELL = 'Internal Shell'      # option inside Boot Manager
BIOS_NAV_MAX   = 20                    # maximum down-arrow presses before error
BIOS_ARROW_DELAY = 1.5                 # wait between DOWN presses to avoid overshooting menu items
BIOS_ENTER_CONFIRM_DELAY = 2.5         # wait before ENTER to let menu highlight settle
BIOS_POST_DETECT_WAIT = 15             # wait after BIOS/F2 screen before parsing menus

# EFI Shell prompts (adjust if they differ on your platform)
EFI_PROMPTS   = [b'Shell>', b'shell>', b'EFI Shell']
SVOS_PROMPT   = b'root@'  # SVOS shell prompt prefix (hostname can vary: sut, dhcp1, etc.)
SVOS_LOGIN_PROMPTS = [b' login:', b'login:', b'Login:']
CENTOS_LOGIN_PROMPTS = [b'dmr-bkc login:', b' login:']
CENTOS_SHELL_PROMPTS = [b'# ', b'root@', b'[root@']

# Maximum time (seconds) to wait for long prompts
BOOT_TIMEOUT     = 600   # boot until EFI shell (post-BIOS)       10 min
BIOS_REBOOT_WAIT = 10    # minimum wait before looking for BIOS (flush buffer)
BIOS_WAIT_TIMEOUT= 900   # BIOS screen timeout before retry    15 min
BIOS_NUDGE_INTERVAL = 5  # seconds between refresh keys if BIOS is static
SVOS_TIMEOUT     = 600   # boot SVOS                               10 min
CENTOS_BOOT_TIMEOUT = 600  # boot CentOS                           10 min
MOUNTSV_TIMEOUT  = 1800  # mountsv                                 30 min
CMD_TIMEOUT      = 120   # comandos normales                        2 min
SC_TIMEOUT       = 600   # supercollider -M 5                      10 min
ROCKET_TIMEOUT   = 1200  # rocket + rtm por config                 20 min
MEMIC_TIMEOUT    = 2400  # memicals                                40 min
MLC_TIMEOUT      = 2400  # mlc                                     40 min
SOLAR_TIMEOUT    = 1200  # solar                                   20 min
OSVOSUPDATE_TIMEOUT = 900   # osvosupdate -v                        15 min
SVOSINFO_TIMEOUT    = 60    # svosinfo                               1 min
UPDATE_MOUNTSV_TIMEOUT = 900  # umountsv;mountsv in Update SVOS     15 min
NO_KILL_TIME = False


_TIMEOUT_DEFAULTS = {
    'BOOT_TIMEOUT': BOOT_TIMEOUT,
    'BIOS_REBOOT_WAIT': BIOS_REBOOT_WAIT,
    'BIOS_WAIT_TIMEOUT': BIOS_WAIT_TIMEOUT,
    'BIOS_NUDGE_INTERVAL': BIOS_NUDGE_INTERVAL,
    'SVOS_TIMEOUT': SVOS_TIMEOUT,
    'CENTOS_BOOT_TIMEOUT': CENTOS_BOOT_TIMEOUT,
    'MOUNTSV_TIMEOUT': MOUNTSV_TIMEOUT,
    'CMD_TIMEOUT': CMD_TIMEOUT,
    'SC_TIMEOUT': SC_TIMEOUT,
    'ROCKET_TIMEOUT': ROCKET_TIMEOUT,
    'MEMIC_TIMEOUT': MEMIC_TIMEOUT,
    'MLC_TIMEOUT': MLC_TIMEOUT,
    'SOLAR_TIMEOUT': SOLAR_TIMEOUT,
    'OSVOSUPDATE_TIMEOUT': OSVOSUPDATE_TIMEOUT,
    'SVOSINFO_TIMEOUT': SVOSINFO_TIMEOUT,
    'UPDATE_MOUNTSV_TIMEOUT': UPDATE_MOUNTSV_TIMEOUT,
}


def _ensure_timeouts_file():
    if TIMEOUTS_CONFIG_FILE.exists():
        return
    payload = {
        '_comment': 'Edit timeout values in seconds. Positive integers only.',
        **_TIMEOUT_DEFAULTS,
    }
    TIMEOUTS_CONFIG_FILE.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def _apply_timeouts_from_file():
    """Loads timeout values from timeouts_config.json and applies them globally."""
    global BOOT_TIMEOUT, BIOS_REBOOT_WAIT, BIOS_WAIT_TIMEOUT, BIOS_NUDGE_INTERVAL
    global SVOS_TIMEOUT, CENTOS_BOOT_TIMEOUT, MOUNTSV_TIMEOUT, CMD_TIMEOUT
    global SC_TIMEOUT, ROCKET_TIMEOUT, MEMIC_TIMEOUT, MLC_TIMEOUT, SOLAR_TIMEOUT
    global OSVOSUPDATE_TIMEOUT, SVOSINFO_TIMEOUT, UPDATE_MOUNTSV_TIMEOUT

    _ensure_timeouts_file()

    try:
        data = json.loads(TIMEOUTS_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        _status(f'Could not read {TIMEOUTS_CONFIG_FILE.name}: {e}. Using defaults.', 'warn')
        return

    for key, default_val in _TIMEOUT_DEFAULTS.items():
        raw = data.get(key, default_val)
        try:
            val = int(raw)
            if val <= 0:
                raise ValueError('must be > 0')
        except Exception:
            _status(f'Invalid {key}={raw!r} in {TIMEOUTS_CONFIG_FILE.name}. Keeping {default_val}.', 'warn')
            val = default_val
        globals()[key] = val

    _status(f'Timeouts loaded from {TIMEOUTS_CONFIG_FILE.name}.', 'info')


def _enable_no_kill_time_mode():
    """Disables timeout-based kills for validation runs."""
    timeout_keys = [k for k in _TIMEOUT_DEFAULTS.keys() if k.endswith('_TIMEOUT')]
    for key in timeout_keys:
        globals()[key] = None
    _status('Validation mode active: timeout kills disabled (No Kill Time).', 'warn')


def _ask_runtime_profile():
    """Prompts startup execution profile: normal or validation without timeout kills."""
    global NO_KILL_TIME
    print()
    print('=' * 60)
    print('  EXECUTION PROFILE')
    print('=' * 60)
    print('  1 - Normal')
    print('  2 - Validation (No Kill Time)')
    print()
    while True:
        raw = input('Profile (1-2): ').strip()
        if raw == '1':
            NO_KILL_TIME = False
            return
        if raw == '2':
            NO_KILL_TIME = True
            _enable_no_kill_time_mode()
            return
        print('  [!!] Enter 1 or 2.')

# Rockets that run in the normal flow (cpu and iax)
ROCKET_CMDS = [
    ('rocket --cfgs --atlas "--hw dram,cpu" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_cpu.txt', 'rocket_dram_cpu'),
    ('rocket --cfgs --atlas "--hw dram,iax" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_iax.txt', 'rocket_dram_iax'),
]

# Rocket dsa/vtd: try direct fast path first; on failure/unknown use contention recovery sequence.
ROCKET_DSA_CMD = ('rocket --cfgs --atlas "--hw dram,dsa,vtd" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_dsa.txt', 'rocket_dram_dsa')

SOLAR_CMD = ('/usr/bin/solar/solar.sh /meshgv '
             '-ratioPUnit0 "" -ratioPUnit1 "" -ratioPUnit2 P0...Pn '
             '-ratioPUnit3 "" -ratioPUnit4 "" -ratioPUnit5 P0...Pn '
             '-ratioPUnit2f1 P0...Pn -ratioPUnit5f1 P0...Pn /log .')

SVOS_GRUB_PATH = '\\efi\\debian\\grubx64.efi'
CENTOS_GRUB_PATH = '\\efi\\centos\\grubx64.efi'

# Canonical content keys (display order for the user)
CONTENT_TESTS = ('supercollider', 'rocket', 'memicals', 'mlc', 'solar', 'svos_boot', 'centos_boot')

# Display names per content key
_CONTENT_DISPLAY = {
    'supercollider': 'SuperCollider',
    'rocket':        'Rocket (cpu/iax/dsa)',
    'memicals':      'Memicals',
    'mlc':           'MLC',
    'solar':         'Solar',
    'svos_boot':     'SVOS Boot (svosinfo response check)',
    'centos_boot':   'CentOS Boot (root/root + ifconfig)',
}

# Display commands for the result log (without file redirection)
CONTENT_CMDS = {
    'supercollider':   'sc -M 5',
    'rocket_dram_cpu': 'rocket --cfgs --atlas "--hw dram,cpu" -M 5; rtm -c rtm.cfg -M 5',
    'rocket_dram_iax': 'rocket --cfgs --atlas "--hw dram,iax" -M 5; rtm -c rtm.cfg -M 5',
    'rocket_dram_dsa': 'rocket --cfgs --atlas "--hw dram,dsa,vtd" -M 5; rtm -c rtm.cfg -M 5',
    'memicals':        'memic.py -M 15 memicals:high-mem:proc -X proc:0,1,2,3',
    'mlc':             'mlc --loaded_latency -t60 -Mdatapattern_halfA_half5.txt',
    'solar':           ('/usr/bin/solar/solar.sh /meshgv -ratioPUnit0 "" -ratioPUnit1 "" '
                        '-ratioPUnit2 P0...Pn -ratioPUnit3 "" -ratioPUnit4 "" '
                        '-ratioPUnit5 P0...Pn -ratioPUnit2f1 P0...Pn -ratioPUnit5f1 P0...Pn /log .'),
    'svos_boot':       'Boot Validation',
    'centos_boot':     'Boot Validation',
}


# ---------------------------------------------------------------------------
# File signal helpers (coordination with sv_automation)
# ---------------------------------------------------------------------------

def _wait_for_file(filepath, poll: float = 10):
    """Waits indefinitely until the file exists."""
    while not Path(filepath).exists():
        time.sleep(poll)


def _wait_for_any_file(filepaths, poll: float = 1.0):
    """Waits indefinitely until any file exists. Returns the first path that appears."""
    paths = [Path(p) for p in filepaths]
    while True:
        for path in paths:
            if path.exists():
                return path
        time.sleep(poll)


def _wait_for_file_timeout(filepath, poll: float = 5, timeout: float = 60) -> bool:
    """Waits up to timeout seconds. Returns True if it arrived, False if it expired."""
    deadline = time.time() + timeout
    while not Path(filepath).exists():
        if time.time() > deadline:
            return False
        time.sleep(poll)
    return True


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def _fmt_dur(secs: float) -> str:
    """Formats seconds as 'Xm Ys' or 'Xh Ym Zs'."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f'{h}h {m:02d}m {s:02d}s'
    return f'{m}m {s:02d}s'


def _fmt_hms(secs: float) -> str:
    """Formats seconds as HH:MM:SS."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def _status(msg: str, level: str = 'step'):
    """Prints a status message with clear formatting to the console."""
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    icons = {'step': '>>>', 'ok': '[OK]', 'wait': '...', 'fail': '[!!]', 'info': '   ', 'warn': '[!]'}
    icon = icons.get(level, '>>>')
    line = f'[{ts}] {icon} {msg}'
    print(line, flush=True)
    logging.info(msg)


# Test mode: activates pauses between tests.
TEST_MODE   = False
# Times each step and shows it in the final logs (always active).
CRONOS_MODE = True
_timings: dict = {}  # {qdf: {step: elapsed_seconds}}


class _NullRuntimeMonitor:
    """No-op monitor used when popup UI is unavailable."""
    def set_tool(self, _name: str):
        return

    def start_stage(self, _name: str):
        return

    def end_stage(self, _status: str = 'DONE'):
        return


class _PopupRuntimeMonitor:
    """Non-blocking popup monitor that tracks live stage durations."""
    def __init__(self):
        self._events = pyqueue.Queue()
        self._lock = threading.Lock()
        self._stages = []
        self._current_idx = None
        self._tool_name = 'Waiting for tool...'
        self._last_render_text = ''
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._pinned = False
        self._allow_close = False
        self._thread = threading.Thread(target=self._ui_worker, daemon=True)
        self._thread.start()

        # Give the UI a short startup window; if unavailable, auto-disable.
        self._ready.wait(timeout=2.0)

    def _ui_worker(self):
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title('FCO AutoTool - Runtime Monitor')
            root.geometry('560x320')
            root.protocol('WM_DELETE_WINDOW', self._on_close_request)
            self._root = root
            self._set_topmost(True)
            root.after(3000, self._release_initial_topmost)

            container = ttk.Frame(root, padding=10)
            container.pack(fill='both', expand=True)

            header = ttk.Frame(container)
            header.pack(fill='x')

            title = ttk.Label(header, text='Live execution monitor (Boot SVOS v1)', font=('Segoe UI', 10, 'bold'))
            title.pack(side='left', anchor='w')

            self._pin_button = ttk.Button(header, text='Pin', width=8, command=self._toggle_pin)
            self._pin_button.pack(side='right')

            text_frame = ttk.Frame(container)
            text_frame.pack(fill='both', expand=True, pady=(8, 0))

            self._text = tk.Text(text_frame, height=14, width=78, state='disabled')
            self._text.pack(side='left', fill='both', expand=True)

            self._scrollbar = ttk.Scrollbar(text_frame, orient='vertical', command=self._text.yview)
            self._scrollbar.pack(side='right', fill='y')
            self._text.configure(yscrollcommand=self._scrollbar.set)

            self._text.bind('<Control-c>', self._copy_selection)
            self._text.bind('<Control-C>', self._copy_selection)

            self._ready.set()

            def _tick():
                self._drain_events()
                self._render()
                root.after(500, _tick)

            root.after(200, _tick)
            root.mainloop()
        except Exception:
            self._failed.set()
            self._ready.set()

    def _on_close_request(self):
        """Keeps monitor alive while the tool is still running."""
        if self._allow_close:
            try:
                self._root.destroy()
            except Exception:
                pass
            return
        _status('Runtime monitor stays open until the tool closes (Ctrl+C or normal exit).', 'info')

    def _set_topmost(self, enabled: bool):
        if not hasattr(self, '_root'):
            return
        try:
            self._root.attributes('-topmost', enabled)
            if enabled:
                self._root.lift()
        except Exception:
            pass

    def _release_initial_topmost(self):
        if self._pinned:
            return
        self._set_topmost(False)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._set_topmost(self._pinned)
        if hasattr(self, '_pin_button'):
            self._pin_button.configure(text='Unpin' if self._pinned else 'Pin')

    def _copy_selection(self, _event=None):
        """Copies current selection to clipboard (works even while monitor updates)."""
        if not hasattr(self, '_text'):
            return 'break'
        try:
            selected = self._text.get('sel.first', 'sel.last')
        except Exception:
            return 'break'
        if not selected:
            return 'break'
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(selected)
            self._root.update_idletasks()
        except Exception:
            pass
        return 'break'

    def _drain_events(self):
        while True:
            try:
                ev = self._events.get_nowait()
            except pyqueue.Empty:
                break

            kind = ev.get('kind')
            now = time.time()
            with self._lock:
                if kind == 'start':
                    if self._current_idx is not None:
                        # Defensive close if caller forgot to close previous stage.
                        cur = self._stages[self._current_idx]
                        if cur['end'] is None:
                            cur['end'] = now
                            cur['status'] = 'DONE'
                    self._stages.append({'name': ev['name'], 'start': now, 'end': None, 'status': 'RUNNING'})
                    self._current_idx = len(self._stages) - 1
                elif kind == 'tool':
                    self._tool_name = ev.get('name') or 'Waiting for tool...'
                elif kind == 'end' and self._current_idx is not None:
                    cur = self._stages[self._current_idx]
                    if cur['end'] is None:
                        cur['end'] = now
                    cur['status'] = ev.get('status', 'DONE')
                    self._current_idx = None

    def _render(self):
        if not hasattr(self, '_text'):
            return

        now = time.time()
        lines = []
        with self._lock:
            lines.append(f'Tool: {self._tool_name}')
            lines.append('')
            if not self._stages:
                lines.append('Waiting for first stage...')
            else:
                for idx, stage in enumerate(self._stages, start=1):
                    end_ts = stage['end'] if stage['end'] is not None else now
                    elapsed = max(0, int(end_ts - stage['start']))
                    timer = _fmt_hms(elapsed)
                    status = stage['status']
                    lines.append(f'{idx}. {stage["name"]}: {timer} [{status}]')

        content = '\n'.join(lines)

        # Keep selection stable while user is copying text from the monitor.
        try:
            if self._text.tag_ranges('sel'):
                return
        except Exception:
            pass

        # Avoid unnecessary rewrites that would reset cursor/selection state.
        if content == self._last_render_text:
            try:
                if not self._text.tag_ranges('sel'):
                    self._text.see('end')
            except Exception:
                pass
            return

        self._text.configure(state='normal')
        self._text.delete('1.0', 'end')
        self._text.insert('1.0', content)
        self._text.configure(state='disabled')
        self._last_render_text = content
        try:
            if not self._text.tag_ranges('sel'):
                self._text.see('end')
        except Exception:
            pass

    def start_stage(self, name: str):
        if self._failed.is_set() or (not self._ready.is_set()):
            return
        self._events.put({'kind': 'start', 'name': name})

    def set_tool(self, name: str):
        if self._failed.is_set() or (not self._ready.is_set()):
            return
        self._events.put({'kind': 'tool', 'name': name})

    def end_stage(self, status: str = 'DONE'):
        if self._failed.is_set() or (not self._ready.is_set()):
            return
        self._events.put({'kind': 'end', 'status': status})


_RUNTIME_MONITOR = None


def _get_runtime_monitor():
    """Returns a lazy singleton runtime monitor (popup if available)."""
    global _RUNTIME_MONITOR
    if _RUNTIME_MONITOR is None:
        mon = _PopupRuntimeMonitor()
        if mon._failed.is_set():
            _status('Runtime monitor popup unavailable. Continuing without popup monitor.', 'warn')
            _RUNTIME_MONITOR = _NullRuntimeMonitor()
        else:
            _status('Runtime monitor popup started.', 'info')
            _RUNTIME_MONITOR = mon
    return _RUNTIME_MONITOR


def _set_runtime_monitor_tool(tool_name: str):
    """Sets the top-level tool label shown in the popup monitor."""
    mon = _get_runtime_monitor()
    mon.set_tool(tool_name)


@contextlib.contextmanager
def _monitor_stage(stage_name: str):
    """Wraps a stage and reports live timing to the popup monitor."""
    mon = _get_runtime_monitor()
    mon.start_stage(stage_name)
    try:
        yield
    except Exception:
        mon.end_stage('FAIL')
        raise
    else:
        mon.end_stage('DONE')


def _pause(msg: str = 'Press any key to continue...'):
    """Pauses execution only in TEST_MODE."""
    if not TEST_MODE:
        return
    print(f'\n  [PAUSE] {msg}', flush=True)
    if sys.platform == 'win32':
        import msvcrt
        msvcrt.getch()
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print()


def _hold_open_until_interrupt(title: str):
    """Keeps the tool alive after boot until user interrupts manually."""
    print()
    print('=' * 60)
    print(f'  {title} READY')
    print('=' * 60)
    print('  Session is kept open. Press Ctrl+C to close this tool.')
    print()
    while True:
        time.sleep(1)


def _hold_open_on_error(title: str):
    """Keeps the tool open after a fatal error so the user can inspect the console."""
    print()
    print('=' * 60)
    print(f'  {title} FAILED')
    print('=' * 60)
    print('  Process stopped due to an error/timeout.')
    print('  Review the console and popup details above.')
    print('  Serial session remains open. Press Ctrl+C to close this tool.')
    print()
    while True:
        time.sleep(1)


def _pause_before_close(title: str = 'FCO AUTOMATION'):
    """Pauses before exit; if stdin is unavailable, keeps the window open."""
    try:
        input('\nPress ENTER to close...')
    except (EOFError, OSError):
        _status('No interactive stdin detected. Keeping window open (Ctrl+C to close)...', 'warn')
        _hold_open_until_interrupt(title)


def _strip_ansi(data: bytes) -> str:
    """Strips ANSI escape codes from BIOS/terminal output."""
    text = data.decode('utf-8', errors='replace')
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b=|\x1b>', '', text)


# EDK2 color pattern for the HIGHLIGHTED item (white on black = selected)
# BIOS sends ESC[37m ESC[40m just before the text of the active item
_HIGHLIGHT_RE = re.compile(
    rb'\x1b\[(?:0m\x1b\[)?37m\x1b\[40m'   # highlighted color sequence
    rb'((?:\x1b\[\d+;\d+H)?[^\x1b\r\n]{1,80})'  # text (with possible cursor position)
)
_CURSOR_POS_RE = re.compile(rb'\x1b\[\d+;\d+H')
_ARROW_ROW_RE = re.compile(rb'\x1b\[(\d+);002H>')


def _highlighted_items(raw: bytes) -> list:
    """
    Returns a list of texts currently HIGHLIGHTED in the BIOS menu.
    EDK2 uses ESC[37mESC[40m (white on black) for the selected item.
    """
    results = []
    for m in _HIGHLIGHT_RE.finditer(raw):
        chunk = _CURSOR_POS_RE.sub(b'', m.group(1))
        text = chunk.decode('utf-8', errors='replace').strip()
        if text:
            results.append(text)
    return results


def _selected_items_by_arrow(raw: bytes) -> list:
    """
    Returns selected item texts using BIOS arrow marker '>' at column 2.
    This is more reliable than color parsing on some noisy ANSI redraws.
    """
    results = []
    for m in _ARROW_ROW_RE.finditer(raw):
        row = m.group(1).decode('ascii', errors='ignore')
        # Item text is rendered at column 4 on the same row.
        pat = re.compile(rb'\x1b\[' + row.encode() + rb';004H([^\x1b\r\n]{1,80})')
        m_text = pat.search(raw)
        if not m_text:
            continue
        text = m_text.group(1).decode('utf-8', errors='replace').strip()
        if text:
            results.append(text)
    return results


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = '%(asctime)s [%(levelname)s] %(message)s'
    logging.basicConfig(
        level   = logging.INFO,
        format  = fmt,
        handlers= [
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )


# ---------------------------------------------------------------------------
# Keypress detection for timeout skip (Windows only)
# ---------------------------------------------------------------------------

def _check_skip_key():
    """
    Checks if user pressed 's'/'S' to skip BIOS wait timeout.
    Returns True only for 's' or 'S'.
    Only works on Windows with msvcrt.
    """
    if sys.platform != 'win32':
        return False
    
    if msvcrt.kbhit():
        key = msvcrt.getch()
        if key in (b's', b'S'):
            _status("[USER SKIP] BIOS wait interrupted by 's'", 'step')
            return True
    
    return False


def _sanitize_log_token(value: str) -> str:
    """Sanitizes a string for use in log filenames."""
    token = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '').strip())
    return token.strip('._-') or 'session'


# ---------------------------------------------------------------------------
# Serial helper
# ---------------------------------------------------------------------------

class SVOSSession:
    """Wraps pyserial with read_until / send helpers."""

    def __init__(self, port, baudrate=BAUDRATE):
        self.port    = port
        self.ser     = serial.Serial(port, baudrate, timeout=0.1)
        self.log     = logging.getLogger('serial')
        self.buf     = b''
        self._capture_stack = []

    def begin_serial_capture(self, phase_name: str):
        """Starts a serial transcript capture for a specific boot phase."""
        capture = {
            'phase': _sanitize_log_token(phase_name),
            'started_at': datetime.datetime.now(),
            'parts': [],
        }
        self._capture_stack.append(capture)
        return capture

    def end_serial_capture(self):
        """Ends the latest serial transcript capture and writes it to disk."""
        if not self._capture_stack:
            return None
        capture = self._capture_stack.pop()
        SERIAL_SEGMENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = capture['started_at'].strftime('%Y%m%d_%H%M%S_%f')
        port_token = _sanitize_log_token(self.port)
        out = SERIAL_SEGMENT_LOG_DIR / f'{ts}_{port_token}_{capture["phase"]}.log'
        header = [
            f'Date/Time: {capture["started_at"].strftime("%Y-%m-%d %H:%M:%S")}',
            f'COM: {self.port}',
            f'Phase: {capture["phase"]}',
            '-' * 70,
            '',
        ]
        out.write_text('\n'.join(header) + ''.join(capture['parts']), encoding='utf-8')
        return out

    def _capture_text(self, text: str):
        if not text or not self._capture_stack:
            return
        for capture in self._capture_stack:
            capture['parts'].append(text)

    def _capture_tx(self, text: str):
        self._capture_text(f'\n[TX] {text}\n')

    def send_arrow_down(self):
        """Sends the down arrow key (ANSI escape)."""
        self.ser.write(b'\x1b[B')

    def send_arrow_up(self):
        """Sends the up arrow key (ANSI escape)."""
        self.ser.write(b'\x1b[A')

    def read_screen(self, wait=0.4):
        """
        Reads whatever is in the buffer after a pause.
        Returns (raw_bytes, clean_text).
        """
        time.sleep(wait)
        raw = b''
        while True:
            chunk = self.ser.read(1024)
            if not chunk:
                break
            raw += chunk
        self.buf += raw
        self._print(raw)
        return raw, _strip_ansi(raw)

    # ---- write ----

    def send(self, cmd: str):
        self.log.info(f'>>> {cmd!r}')
        self._capture_tx(repr(cmd))
        self.ser.write((cmd + '\r\n').encode())

    def send_slow(self, cmd: str, char_delay: float = 0.05):
        """
        Sends the command character by character with a delay between each one.
        Use when the terminal cannot process fast input (e.g.: EFI Shell).
        """
        self.log.info(f'>>> (slow) {cmd!r}')
        self._capture_tx(f'(slow) {cmd!r}')
        for ch in cmd:
            self.ser.write(ch.encode())
            time.sleep(char_delay)
        time.sleep(0.1)  # pause before Enter
        self.ser.write(b'\r\n')

    def send_enter(self):
        self._capture_tx('<ENTER>')
        self.ser.write(b'\r\n')

    def send_escape(self):
        """Sends the ESC key to go back to the previous menu in BIOS."""
        self._capture_tx('<ESC>')
        self.ser.write(b'\x1b')

    def send_key(self, raw: bytes):
        self._capture_tx(f'<RAW {raw!r}>')
        self.ser.write(raw)

    # ---- read ----

    def read_until(self, expected, timeout=CMD_TIMEOUT) -> bytes:
        """Reads until `expected` is found. timeout=None waits indefinitely."""
        if isinstance(expected, str):
            expected = expected.encode()
        deadline = (time.time() + timeout) if timeout is not None else None
        while True:
            chunk = self.ser.read(512)
            if chunk:
                self.buf += chunk
                self._print(chunk)
            if expected in self.buf:
                out, self.buf = self.buf, b''
                return out
            if deadline and time.time() > deadline:
                raise TimeoutError(f'Timeout ({timeout}s) waiting for: {expected!r}')
            time.sleep(0.05)

    def read_until_any(self, patterns, timeout=CMD_TIMEOUT):
        """Reads until any of the patterns is found. timeout=None waits indefinitely."""
        enc = [p.encode() if isinstance(p, str) else p for p in patterns]
        deadline = (time.time() + timeout) if timeout is not None else None
        while True:
            chunk = self.ser.read(512)
            if chunk:
                self.buf += chunk
                self._print(chunk)
            for p in enc:
                if p in self.buf:
                    out, self.buf = self.buf, b''
                    return p, out
            if deadline and time.time() > deadline:
                raise TimeoutError(f'Timeout ({timeout}s) waiting for patterns: {patterns}')
            time.sleep(0.05)

    def flush(self):
        """Flushes the read buffer."""
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.buf = b''

    def close(self):
        self.ser.close()

    def _print(self, data: bytes):
        try:
            text = data.decode('utf-8', errors='replace')
            self._capture_text(text)
            print(text, end='', flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# BIOS navigation
# ---------------------------------------------------------------------------

def navigate_bios_menu(s: SVOSSession, target: str, max_steps: int = BIOS_NAV_MAX,
                       arrow_delay: float = BIOS_ARROW_DELAY):
    """
    Navigates the BIOS menu with down arrows looking for `target` as the HIGHLIGHTED item.
    EDK2 highlights the active item with ANSI color ESC[37mESC[40m (white on black).
    When found: waits briefly, then re-checks highlight in short bursts.
    If confirmation is inconclusive (common with noisy ANSI redraws),
    trusts the initial hit and presses Enter.
    Raises FCOStepError if not found within max_steps attempts.
    """
    for attempt in range(max_steps):
        raw, _ = s.read_screen(wait=0.5)
        highlighted = _highlighted_items(raw)
        selected_by_arrow = _selected_items_by_arrow(raw)
        _status(f'Attempt {attempt+1}/{max_steps} - highlighted: {highlighted} | arrow: {selected_by_arrow}', 'wait')

        # Some BIOS frames render multiple '>' markers (menu bullets), which is ambiguous.
        arrow_is_reliable = len(selected_by_arrow) == 1
        if (not arrow_is_reliable) and selected_by_arrow:
            _status('Arrow parse ambiguous (multiple entries). Ignoring arrow for selection decision.', 'info')

        initial_match = any(target in h for h in highlighted) or (
            arrow_is_reliable and any(target in h for h in selected_by_arrow)
        )
        if initial_match:
            _status(f'"{target}" is highlighted -> confirming for {BIOS_ENTER_CONFIRM_DELAY:.1f}s before Enter', 'ok')
            time.sleep(BIOS_ENTER_CONFIRM_DELAY)

            # Re-check in multiple short snapshots; parser can miss highlights
            # during transient EDKII redraw frames.
            seen_target = False
            saw_any_highlight = False
            saw_any_arrow = False
            confirm_seen = []
            confirm_arrow_seen = []
            for _ in range(3):
                raw_confirm, _ = s.read_screen(wait=0.2)
                highlighted_confirm = _highlighted_items(raw_confirm)
                arrow_confirm = _selected_items_by_arrow(raw_confirm)
                arrow_confirm_reliable = len(arrow_confirm) == 1
                confirm_seen.extend(highlighted_confirm)
                confirm_arrow_seen.extend(arrow_confirm)
                if highlighted_confirm:
                    saw_any_highlight = True
                if arrow_confirm_reliable and arrow_confirm:
                    saw_any_arrow = True
                if any(target in h for h in highlighted_confirm) or (
                    arrow_confirm_reliable and any(target in h for h in arrow_confirm)
                ):
                    seen_target = True
                    break

            _status(f'Confirmation highlight: {confirm_seen} | arrow: {confirm_arrow_seen}', 'info')
            if (saw_any_highlight or saw_any_arrow) and not seen_target:
                _status(f'"{target}" moved before Enter. Continuing navigation...', 'warn')
                continue
            if (not saw_any_highlight) and (not saw_any_arrow) and (not seen_target):
                _status('Confirmation inconclusive (no highlight parsed). Trusting initial hit and pressing Enter...', 'warn')

            s.send_enter()
            time.sleep(0.5)
            return

        s.send_arrow_down()
        time.sleep(arrow_delay)

    raise FCOStepError(f'Could not find "{target}" highlighted in BIOS menu after {max_steps} attempts.')


BIOS_NAV_RETRIES = 5  # maximum retries with ESC before giving up

# Internal Shell countdown handling (to prevent startup.nsh auto-jump to FS0)
INT_SHELL_COUNTDOWN_TIMEOUT = 25  # seconds
INT_SHELL_PRE_ESC_WAIT = 1        # seconds to wait after selecting Internal Shell before ESC
INT_SHELL_POST_ESC_WAIT = 5       # seconds to stabilize after ESC before parsing Shell prompt
INT_SHELL_COUNTDOWN_HINTS = [
    b'Press ESC',
    b'press esc',
    b'startup.nsh',
    b'to skip startup.nsh',
]


def _wait_for_bios_with_nudge(s: SVOSSession, timeout: int | None, enable_nudge: bool = False):
    """
    Waits for the BIOS screen.
    If enable_nudge=True and no data arrives (static BIOS), sends a refresh key
    every BIOS_NUDGE_INTERVAL seconds to force redraw over serial.
    """
    nudge_key = b'\x1b[B'  # down arrow
    deadline = (time.time() + timeout) if timeout is not None else None

    while True:
        if _check_skip_key():
            raise BiosTimeoutError("BIOS screen wait skipped by user pressing 's'")

        if deadline is None:
            wait = BIOS_NUDGE_INTERVAL
        else:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BiosTimeoutError(
                    f'BIOS screen did not appear within {timeout}s ({timeout//60} min)')
            wait = min(BIOS_NUDGE_INTERVAL, remaining)
        try:
            s.read_until_any(BIOS_BANNERS, timeout=wait)
            return  # banner encontrado
        except TimeoutError:
            pass

        if deadline is not None and time.time() >= deadline:
            raise BiosTimeoutError(
                f'BIOS screen did not appear within {timeout}s ({timeout//60} min)')

        if enable_nudge:
            _status('Static BIOS screen — sending DOWN arrow to refresh...', 'wait')
            s.send_key(nudge_key)
            time.sleep(0.3)


def _break_internal_shell_countdown(s: SVOSSession, timeout: int = INT_SHELL_COUNTDOWN_TIMEOUT) -> bool:
    """
    Watches serial output right after selecting Internal Shell and sends ESC
    when the startup.nsh countdown appears.

    Returns True if ESC was sent, False otherwise.
    """
    _status('Internal Shell settle: waiting before forced ESC...', 'wait')
    time.sleep(INT_SHELL_PRE_ESC_WAIT)
    _status('Sending ESC to stop startup.nsh auto-run...', 'step')
    s.send_escape()
    esc_sent = True
    deadline = time.time() + timeout

    while time.time() < deadline:
        chunk = s.ser.read(512)
        if chunk:
            s.buf += chunk
            s._print(chunk)

            if any(h in s.buf for h in INT_SHELL_COUNTDOWN_HINTS):
                _status('Countdown text observed after ESC.', 'info')

            low = s.buf.lower()
            if b'shell>' in low or b'efi shell' in low or b'fs0:' in low or b'fs1:' in low or b'fs2:' in low:
                break

        time.sleep(0.05)

    _status('ESC sent for Internal Shell countdown.', 'ok')
    return esc_sent


def _read_until_any_with_periodic_enter(s: SVOSSession, patterns, timeout=SVOS_TIMEOUT,
                                        enter_every: int = 5,
                                        tick_msg: str = 'Waiting... sending ENTER keepalive...'):
    """Reads until any pattern is found, sending ENTER periodically while waiting."""
    if enter_every <= 0:
        enter_every = 5

    enc = [p.encode() if isinstance(p, str) else p for p in patterns]
    deadline = (time.time() + timeout) if timeout is not None else None
    next_enter = time.time() + enter_every

    while True:
        chunk = s.ser.read(512)
        if chunk:
            s.buf += chunk
            s._print(chunk)

        for p in enc:
            if p in s.buf:
                out, s.buf = s.buf, b''
                return p, out

        now = time.time()
        if deadline is not None and now > deadline:
            raise TimeoutError(f'Timeout ({timeout}s) waiting for patterns: {patterns}')

        if now >= next_enter:
            _status(tick_msg, 'info')
            s.send_enter()
            next_enter = now + enter_every

        time.sleep(0.05)


def boot_svos(s: SVOSSession, do_mountsv: bool = True, fused_nudge: bool = False):
    """
    Secuencia completa de arranque:
      BIOS (OAKSTREAM) -> Boot Manager Menu -> UEFI Internal Shell
            -> FS0/FS1/FS2 -> \\efi\\debian\\grubx64.efi -> ENTER (ATTENTION) -> login
      -> mountsv (solo si do_mountsv=True)
    """
    s.begin_serial_capture('boot_to_efi')
    boot_to_efi_log = None
    try:
        with _monitor_stage('Boot to EFI'):
            # 1. Wait for the BIOS screen (the system needs time to reboot)
            _status(f'Waiting for system reboot ({BIOS_REBOOT_WAIT}s)...', 'wait')
            time.sleep(BIOS_REBOOT_WAIT)
            s.flush()  # flush accumulated buffer during reboot

            _status('Looking for BIOS screen...', 'wait')
            _status("(Press 's' to skip BIOS wait and continue failure handling)", 'info')
            if fused_nudge:
                _status('(Fused flow: DOWN arrow can be sent automatically to refresh static BIOS)', 'info')
            _wait_for_bios_with_nudge(s, BIOS_WAIT_TIMEOUT, enable_nudge=fused_nudge)
            _status('BIOS detected.', 'ok')
            _status(f'Waiting {BIOS_POST_DETECT_WAIT}s for BIOS menu to stabilize before navigation...', 'wait')
            time.sleep(BIOS_POST_DETECT_WAIT)

            # 2+3. Navigate Boot Manager Menu -> UEFI Internal Shell with retry via ESC
            for nav_retry in range(BIOS_NAV_RETRIES):
                try:
                    _status(f'Looking for Boot Manager Menu (attempt {nav_retry+1}/{BIOS_NAV_RETRIES})...', 'wait')
                    navigate_bios_menu(s, BIOS_BOOT_MGR, arrow_delay=BIOS_ARROW_DELAY)

                    # Verify that we entered Boot Manager Menu and not another menu.
                    # Use Internal Shell presence as positive signal to avoid false
                    # negatives caused by transient/partial screen redraw text.
                    time.sleep(1.0)
                    raw, screen_text = s.read_screen(wait=1.0)
                    internal_shell_visible = ('Internal Shell' in screen_text) or ('UEFI Internal Shell' in screen_text)
                    if not internal_shell_visible:
                        _status('Entered Boot Maintenance Manager by mistake. Sending ESC...', 'warn')
                        s.send_escape()
                        time.sleep(1.5)
                        continue  # retry from the top of the loop
                    _status('Inside Boot Manager Menu.', 'ok')

                    _status(f'Looking for UEFI Internal Shell...', 'step')
                    navigate_bios_menu(s, BIOS_INT_SHELL, arrow_delay=BIOS_ARROW_DELAY)
                    _status('UEFI Internal Shell selected.', 'ok')
                    break  # success, exit the retry loop

                except FCOStepError as e:
                    if nav_retry < BIOS_NAV_RETRIES - 1:
                        _status(f'Not found ({e}). ESC and retrying...', 'warn')
                        s.send_escape()
                        time.sleep(1.5)
                    else:
                        raise FCOStepError(
                            f'Could not navigate BIOS after {BIOS_NAV_RETRIES} intentos. '
                            f'Last error: {e}')

        # 4. Break Internal Shell countdown (if present), then wait for prompt
        _break_internal_shell_countdown(s)
        _status(f'Post-ESC settle wait: {INT_SHELL_POST_ESC_WAIT}s before Shell prompt search...', 'wait')
        time.sleep(INT_SHELL_POST_ESC_WAIT)
        _status('Sending ENTER after post-ESC settle...', 'step')
        s.send_enter()
        _status('Waiting for EFI Shell...', 'wait')
        with _guard('EFI Shell prompt'):
            matched_prompt, _ = s.read_until_any(EFI_PROMPTS + [b'FS0:', b'FS1:', b'FS2:'], timeout=BOOT_TIMEOUT)
        matched_txt = matched_prompt.decode('utf-8', errors='replace') if isinstance(matched_prompt, bytes) else str(matched_prompt)
        _status(f'EFI Shell ready. Detected prompt token: {matched_txt!r}', 'ok')
    finally:
        boot_to_efi_log = s.end_serial_capture()
        if boot_to_efi_log:
            _status(f'Serial boot log saved: {boot_to_efi_log}', 'info')

    # 5. Look for SVOS grub on FS0, FS1, FS2
    s.begin_serial_capture('efi_to_svos')
    efi_to_svos_log = None
    booted = False
    try:
        for fs in ('FS0', 'FS1', 'FS2'):
            _status(f'Trying {fs}: ...', 'step')
            s.flush()
            s.send(f'{fs}:')
            try:
                s.read_until(f'{fs}:\\', timeout=15)
            except TimeoutError:
                _status(f'{fs}: not available, trying next...', 'info')
                continue

            _status(f'Launching {SVOS_GRUB_PATH} from {fs}: ...', 'step')
            s.send_slow(SVOS_GRUB_PATH, char_delay=0.05)

            # If the file does NOT exist: the EFI shell returns the prompt in < 5s
            # If the file DOES exist: there is no response for several seconds while it loads
            # -> Wait 10s for the shell prompt; if it does not return = it is loading
            ATTENTION  = b'Press <ENTER> within 10 seconds to drop to a login shell'
            EFI_PROMPT = [b'Shell>', b'shell>', f'{fs}:\\'.encode(), f'{fs}:/'.encode()]
            try:
                matched, _ = _read_until_any_with_periodic_enter(
                    s,
                    [ATTENTION] + EFI_PROMPT,
                    timeout=10,
                    enter_every=5,
                    tick_msg='Waiting ATTENTION (grubx64 stage), sending ENTER keepalive...'
                )
                if matched == ATTENTION:
                    booted = True
                    break
                else:
                    # The shell returned the prompt = file not found
                    _status(f'{SVOS_GRUB_PATH} not found on {fs}: (prompt returned quickly), trying next...', 'info')
                    continue
            except TimeoutError:
                # No prompt within 10s = the file loaded and is booting, wait without limit
                _status(f'{SVOS_GRUB_PATH} loading on {fs}:, waiting for ATTENTION without limit (ENTER every 5s)...', 'wait')
                _read_until_any_with_periodic_enter(
                    s,
                    [ATTENTION],
                    timeout=None,
                    enter_every=5,
                    tick_msg='Still waiting ATTENTION, sending ENTER keepalive...'
                )
                booted = True
                break

        if not booted:
            raise FCOStepError(
                f'No se encontro {SVOS_GRUB_PATH} en FS0:, FS1: ni FS2:. '
                'Verifica que el filesystem este disponible.')

        _status('ATTENTION message detected. Sending ENTER...', 'step')
        s.send_enter()

        with _monitor_stage('Boot to SVOS'):
            # 7. Wait for the first root@... prompt (temporary post-ATTENTION shell)
            _status('Loading SVOS...', 'wait')
            with _guard('shell SVOS post-boot (root@... prompt)'):
                s.read_until(SVOS_PROMPT, timeout=SVOS_TIMEOUT)
            _status('Temporary shell ready. Running login...', 'step')
            s.send('login')

            # 8. Login: wait for generic "<hostname> login:" prompt, then send credentials.
            # Some platforms redraw slowly; if the first wait times out, send ENTER and retry once.
            try:
                with _guard('SVOS login prompt (hostname login:) - verify that SVOS loaded correctly'):
                    s.read_until_any(SVOS_LOGIN_PROMPTS, timeout=20)
            except TimeoutError:
                _status('SVOS login prompt not detected yet. Sending ENTER and retrying...', 'warn')
                s.send_enter()
                with _guard('SVOS login prompt retry (hostname login:)'):
                    s.read_until_any(SVOS_LOGIN_PROMPTS, timeout=20)
            _status('Entering user: root', 'step')
            s.send('root')

            with _guard('prompt "Password:"'):
                s.read_until_any(['Password:', 'password:'], timeout=30)
            _status('Entering password...', 'step')
            s.send('svos')

            # 9. Wait for the root@... prompt (authenticated session)
            with _guard('successful login - verify user/password (root/svos)'):
                s.read_until(SVOS_PROMPT, timeout=30)
            _status('Login successful. SVOS shell ready (root@... prompt).', 'ok')
    finally:
        efi_to_svos_log = s.end_serial_capture()
        if efi_to_svos_log:
            _status(f'Serial OS log saved: {efi_to_svos_log}', 'info')

    if not do_mountsv:
        return

    _pause('Login OK — validate in Raritan and press any key to run mountsv...')

    with _monitor_stage('Mount SVOS'):
        # 10. Run mountsv and wait for the prompt to return
        _status('Running mountsv...', 'step')
        s.send('mountsv')
        try:
            s.read_until(SVOS_PROMPT, timeout=MOUNTSV_TIMEOUT)
        except TimeoutError:
            raise MountsvTimeoutError(f'mountsv did not respond within {MOUNTSV_TIMEOUT//60} min')
        _status('mountsv completed. SVOS mounted successfully.', 'ok')


def boot_centos_direct(s: SVOSSession):
    """
    CentOS direct login (TEST mode).
    Assumes CentOS grub is already executing (BIOS navigation skipped).
    Waits for login prompt, enters root/root, runs ifconfig.
    """
    _status('CentOS Direct Login mode (TEST) — waiting for login prompt...', 'wait')
    
    try:
        with _guard('CentOS login prompt (direct mode)'):
            s.read_until_any(CENTOS_LOGIN_PROMPTS, timeout=CENTOS_BOOT_TIMEOUT)
        _status('Login prompt detected.', 'ok')
        
        _status('Entering user: root', 'step')
        s.send('root')
        
        with _guard('prompt "Password:"'):
            s.read_until_any(['Password:', 'password:'], timeout=30)
        _status('Entering password...', 'step')
        s.send('root')
        
        with _guard('CentOS shell prompt after login'):
            s.read_until_any(CENTOS_SHELL_PROMPTS, timeout=60)
        _status('CentOS login successful.', 'ok')
        
        _status('Running ifconfig to validate the OS...', 'step')
        s.send('ifconfig')
        with _guard('ifconfig response in CentOS'):
            s.read_until_any(CENTOS_SHELL_PROMPTS, timeout=60)
        _status('CentOS validated with ifconfig.', 'ok')
        return 'PASS'
        
    except Exception as e:
        _status(f'CentOS Direct login FAILED: {e}', 'fail')
        logging.error(f'CentOS direct login failed: {e}', exc_info=True)
        return 'FAIL'


def boot_centos(s: SVOSSession, fused_nudge: bool = False):
    """
    CentOS boot sequence:
      BIOS -> Boot Manager Menu -> UEFI Internal Shell
            -> FS0/FS1/FS2 -> \\efi\\centos\\grubx64.efi -> login root/root
      -> ifconfig (basic sanity check)
    """
    s.begin_serial_capture('boot_to_efi')
    boot_to_efi_log = None
    try:
        _status(f'Waiting for system reboot ({BIOS_REBOOT_WAIT}s)...', 'wait')
        time.sleep(BIOS_REBOOT_WAIT)
        s.flush()

        _status('Looking for BIOS screen...', 'wait')
        _status("(Press 's' to skip BIOS wait and continue failure handling)", 'info')
        if fused_nudge:
            _status('(Fused flow: DOWN arrow can be sent automatically to refresh static BIOS)', 'info')
        _wait_for_bios_with_nudge(s, BIOS_WAIT_TIMEOUT, enable_nudge=fused_nudge)
        _status('BIOS detected.', 'ok')
        _status(f'Waiting {BIOS_POST_DETECT_WAIT}s for BIOS menu to stabilize before navigation...', 'wait')
        time.sleep(BIOS_POST_DETECT_WAIT)

        for nav_retry in range(BIOS_NAV_RETRIES):
            try:
                _status(f'Looking for Boot Manager Menu (attempt {nav_retry+1}/{BIOS_NAV_RETRIES})...', 'wait')
                navigate_bios_menu(s, BIOS_BOOT_MGR, arrow_delay=BIOS_ARROW_DELAY)

                time.sleep(1.0)
                _, screen_text = s.read_screen(wait=1.0)
                internal_shell_visible = ('Internal Shell' in screen_text) or ('UEFI Internal Shell' in screen_text)
                if not internal_shell_visible:
                    _status('Entered Boot Maintenance Manager by mistake. Sending ESC...', 'warn')
                    s.send_escape()
                    time.sleep(1.5)
                    continue
                _status('Inside Boot Manager Menu.', 'ok')

                _status('Looking for UEFI Internal Shell...', 'step')
                navigate_bios_menu(s, BIOS_INT_SHELL, arrow_delay=BIOS_ARROW_DELAY)
                _status('UEFI Internal Shell selected.', 'ok')
                break

            except FCOStepError as e:
                if nav_retry < BIOS_NAV_RETRIES - 1:
                    _status(f'Not found ({e}). ESC and retrying...', 'warn')
                    s.send_escape()
                    time.sleep(1.5)
                else:
                    raise FCOStepError(
                        f'Could not navigate BIOS after {BIOS_NAV_RETRIES} attempts. '
                        f'Last error: {e}')

        _break_internal_shell_countdown(s)
        _status(f'Post-ESC settle wait: {INT_SHELL_POST_ESC_WAIT}s before Shell prompt search...', 'wait')
        time.sleep(INT_SHELL_POST_ESC_WAIT)
        _status('Sending ENTER after post-ESC settle...', 'step')
        s.send_enter()
        _status('Waiting for EFI Shell...', 'wait')
        with _guard('EFI Shell prompt'):
            matched_prompt, _ = s.read_until_any(EFI_PROMPTS + [b'FS0:', b'FS1:', b'FS2:'], timeout=BOOT_TIMEOUT)
        matched_txt = matched_prompt.decode('utf-8', errors='replace') if isinstance(matched_prompt, bytes) else str(matched_prompt)
        _status(f'EFI Shell ready. Detected prompt token: {matched_txt!r}', 'ok')
    finally:
        boot_to_efi_log = s.end_serial_capture()
        if boot_to_efi_log:
            _status(f'Serial boot log saved: {boot_to_efi_log}', 'info')

    s.begin_serial_capture('efi_to_centos')
    efi_to_centos_log = None
    booted = False
    login_seen = False
    try:
        for fs in ('FS0', 'FS1', 'FS2'):
            _status(f'Trying {fs}: ...', 'step')
            s.flush()
            s.send(f'{fs}:')
            try:
                s.read_until(f'{fs}:\\', timeout=15)
            except TimeoutError:
                _status(f'{fs}: not available, trying next...', 'info')
                continue

            _pause(f'Ready to launch {CENTOS_GRUB_PATH} from {fs}:. Press any key to continue...')
            _status(f'Launching {CENTOS_GRUB_PATH} from {fs}: ...', 'step')
            s.send_slow(CENTOS_GRUB_PATH, char_delay=0.05)

            EFI_PROMPT = [b'Shell>', b'shell>', f'{fs}:\\'.encode(), f'{fs}:/'.encode()]
            try:
                matched, _ = s.read_until_any(CENTOS_LOGIN_PROMPTS + EFI_PROMPT, timeout=10)
                if matched in CENTOS_LOGIN_PROMPTS:
                    booted = True
                    login_seen = True
                    break
                _status(f'{CENTOS_GRUB_PATH} not found on {fs}: (prompt returned quickly), trying next...', 'info')
                continue
            except TimeoutError:
                _status(f'{CENTOS_GRUB_PATH} loading on {fs}:, waiting for login prompt...', 'wait')
                s.read_until_any(CENTOS_LOGIN_PROMPTS, timeout=CENTOS_BOOT_TIMEOUT)
                booted = True
                login_seen = True
                break

        if not booted:
            raise FCOStepError(
                f'Could not find {CENTOS_GRUB_PATH} on FS0:, FS1: or FS2:. '
                'Verify that the filesystem is available.')

        if not login_seen:
            _status('Waiting for CentOS login prompt...', 'wait')
            with _guard('CentOS login prompt'):
                s.read_until_any(CENTOS_LOGIN_PROMPTS, timeout=CENTOS_BOOT_TIMEOUT)

        _status('Entering user: root', 'step')
        s.send('root')

        with _guard('prompt "Password:"'):
            s.read_until_any(['Password:', 'password:'], timeout=30)
        _status('Entering password...', 'step')
        s.send('root')

        with _guard('CentOS shell prompt after login'):
            s.read_until_any(CENTOS_SHELL_PROMPTS, timeout=60)
        _status('CentOS login successful.', 'ok')

        _status('Running ifconfig to validate the OS boot...', 'step')
        s.send('ifconfig')
        with _guard('ifconfig response in CentOS'):
            s.read_until_any(CENTOS_SHELL_PROMPTS, timeout=60)
        _status('CentOS boot validated with ifconfig.', 'ok')
    finally:
        efi_to_centos_log = s.end_serial_capture()
        if efi_to_centos_log:
            _status(f'Serial OS log saved: {efi_to_centos_log}', 'info')


# ---------------------------------------------------------------------------
# Workflow per QDF
# ---------------------------------------------------------------------------


def setup_fco_dir(s: SVOSSession, qdf: str, week: str) -> str:
    """Creates and enters the working directory for the QDF."""
    work_dir = f'/root/FCO/FCO_WW{week}/{qdf}'

    def _read_svos_prompt_resilient(step_desc: str, timeout: int = CMD_TIMEOUT,
                                    retries: int = 1, retry_sleep: float = 0.5) -> bytes:
        """Reads until SVOS prompt, retrying automatically with ENTER on transient stalls."""
        last_err = None
        for attempt in range(retries + 1):
            try:
                attempt_desc = step_desc if attempt == 0 else f'{step_desc} (retry {attempt}/{retries})'
                with _guard(attempt_desc):
                    return s.read_until(SVOS_PROMPT, timeout=timeout)
            except FCOStepError as e:
                last_err = e
                if attempt >= retries:
                    break
                _status(
                    f'{step_desc}: prompt timeout/stall. Sending ENTER and retrying ({attempt + 1}/{retries})...',
                    'warn'
                )
                s.send_enter()
                time.sleep(retry_sleep)
        raise last_err

    _status('Checking SVOS prompt responsiveness before directory setup...', 'wait')
    _read_svos_prompt_resilient('validar prompt SVOS antes de crear directorio', timeout=30, retries=1)

    _status(f'Creating directory: {work_dir}', 'step')
    s.send(f'mkdir -p {work_dir} && cd {work_dir}')
    out_dir = _read_svos_prompt_resilient(f'crear/entrar a {work_dir}', timeout=CMD_TIMEOUT, retries=1)

    # Confirm the shell really moved to the expected directory.
    if work_dir.encode() not in out_dir:
        _status('Directory change not confirmed in command output. Verifying with pwd...', 'warn')
        s.send('pwd')
        out_pwd = _read_svos_prompt_resilient('verificar working dir con pwd', timeout=CMD_TIMEOUT, retries=1)
        if work_dir.encode() not in out_pwd:
            _status('pwd mismatch. Re-entering target directory once...', 'warn')
            s.send(f'cd {work_dir}; pwd')
            out_cd = _read_svos_prompt_resilient('reingresar y verificar working dir', timeout=CMD_TIMEOUT, retries=1)
            if work_dir.encode() not in out_cd:
                raise FCOStepError(f'Could not confirm working directory: {work_dir}')

    # Copy required files for MLC from FCO_Scripts
    _status('Copying mlc and datapattern from ~/FCO_Scripts ...', 'step')
    for f in ['mlc', 'datapattern_halfA_half5.txt']:
        s.send(f'cp ~/FCO_Scripts/{f} .')
        _read_svos_prompt_resilient(f'copy {f} - verify it exists in ~/FCO_Scripts/', timeout=CMD_TIMEOUT, retries=1)
        _status(f'  {f} copied', 'info')
    s.send('chmod +x mlc')
    _read_svos_prompt_resilient('chmod mlc', timeout=CMD_TIMEOUT, retries=1)

    _pause('Setup ready — validate the directory in Raritan and press any key to start tests...')

    _status(f'Directory ready: {work_dir}', 'ok')
    return work_dir


def _recover_svos_prompt_after_timeout(s: SVOSSession, context: str,
                                       attempts: int = 3, prompt_timeout: int = 20) -> bool:
    """Attempts to recover SVOS shell prompt after a timeout with Ctrl+C + ENTER."""
    _status(f'{context}: timeout detected. Attempting SVOS prompt recovery...', 'warn')
    for attempt in range(1, attempts + 1):
        try:
            _status(f'Prompt recovery attempt {attempt}/{attempts} (Ctrl+C + ENTER)...', 'wait')
            s.send_key(b'\x03')
            time.sleep(0.2)
            s.send_enter()
            with _guard(f'prompt recovery after timeout ({context})'):
                s.read_until(SVOS_PROMPT, timeout=prompt_timeout)
            _status(f'{context}: SVOS prompt recovered.', 'ok')
            return True
        except Exception as e:
            _status(f'Recovery attempt {attempt}/{attempts} failed: {e}', 'warn')
            time.sleep(0.4)

    _status(f'{context}: could not recover SVOS prompt after timeout.', 'fail')
    return False


def run_supercollider(s: SVOSSession) -> str:
    _status('Running SuperCollider (sc -M 5)...', 'step')
    s.send('sc -M 5 > sc_out.txt')
    with _guard('SuperCollider - sc -M 5'):
        s.read_until(SVOS_PROMPT, timeout=SC_TIMEOUT)
    s.send('grep -i "TEST PASSED\\|TEST FAILED" sc_out.txt')
    with _guard('parse sc_out.txt'):
        _, buf = s.read_until_any([SVOS_PROMPT], timeout=CMD_TIMEOUT)
    result = 'PASS' if b'TEST PASSED' in buf else ('FAIL' if b'TEST FAILED' in buf else 'UNKNOWN')
    _status(f'SuperCollider: {result}', 'ok' if result == 'PASS' else 'fail')
    _pause(f'SuperCollider {result} — press any key to continue...')
    return result


def _run_rocket_cmd(s: SVOSSession, cmd: str, label: str) -> str:
    """Runs a rocket+rtm command and returns PASS/FAIL/UNKNOWN."""
    _status(f'Running Rocket: {label}...', 'step')
    s.send(cmd)
    with _guard(f'Rocket {label}'):
        s.read_until(SVOS_PROMPT, timeout=ROCKET_TIMEOUT)
    txt = label + '.txt'
    s.send(f'grep -i "test status" {txt}')
    with _guard(f'parse {txt}'):
        _, buf = s.read_until_any([SVOS_PROMPT], timeout=CMD_TIMEOUT)
    result = 'PASS' if b'PASS' in buf.upper() else ('FAIL' if b'FAIL' in buf.upper() else 'UNKNOWN')
    _status(f'Rocket {label}: {result}', 'ok' if result == 'PASS' else 'fail')
    return result


def run_rocket(s: SVOSSession) -> dict:
    """
    Runs Rocket in this order:
      1) dram,cpu
      2) dram,dsa,vtd (fast path only)
      3) dram,iax

    If dsa,vtd fails/unknown, fallback sequence is executed later by run_rocket_dsa()
    to preserve the original end-of-flow behavior.
    """
    results = {}

    # 1) CPU first
    cpu_cmd, cpu_label = ROCKET_CMDS[0]
    with _monitor_stage(f'Rocket {cpu_label}'):
        results[cpu_label] = _run_rocket_cmd(s, cpu_cmd, cpu_label)
    _pause(f'Rocket {cpu_label} {results[cpu_label]} — press any key to continue...')

    # 2) DSA/VTD fast path in the middle (no recovery sequence here)
    dsa_cmd, dsa_label = ROCKET_DSA_CMD
    with _monitor_stage(f'Rocket {dsa_label}'):
        results[dsa_label] = _run_rocket_cmd(s, dsa_cmd, dsa_label)
    _pause(f'Rocket DSA/VTD fast path {results[dsa_label]} — press any key to continue...')

    # 3) IAX last among base Rocket configs
    iax_cmd, iax_label = ROCKET_CMDS[1]
    with _monitor_stage(f'Rocket {iax_label}'):
        results[iax_label] = _run_rocket_cmd(s, iax_cmd, iax_label)
    _pause(f'Rocket {iax_label} {results[iax_label]} — press any key to continue...')

    return results


def run_rocket_dsa(s: SVOSSession, first_result: str = None) -> str:
    """
    Finalizes rocket dram,dsa,vtd result.
    If first_result is PASS, no action is needed.
    If first_result is FAIL/UNKNOWN (or not provided), runs contention recovery:
      killmax -> unmountsv -> rmmodsvos2 -> mountsv -> retry rocket.
    """
    cmd_rocket, label = ROCKET_DSA_CMD

    if first_result is None:
        _status('No previous DSA/VTD result found. Running fast path now...', 'warn')
        first_result = _run_rocket_cmd(s, cmd_rocket, label)

    if first_result == 'PASS':
        _status('Rocket DSA/VTD fast path passed. No fallback needed.', 'ok')
        return first_result

    _status(
        f'Rocket DSA/VTD fast path returned {first_result}. Running contention recovery sequence at end...',
        'warn',
    )
    for prep_cmd in ['killmax', 'unmountsv', 'rmmodsvos2', 'mountsv']:
        _status(f'  Running {prep_cmd}...', 'info')
        s.send(prep_cmd)
        t = MOUNTSV_TIMEOUT if prep_cmd == 'mountsv' else CMD_TIMEOUT
        with _guard(f'{prep_cmd}'):
            s.read_until(SVOS_PROMPT, timeout=t)

    retry_result = _run_rocket_cmd(s, cmd_rocket, label)
    retest_result = f'{retry_result} (Retest)'
    _status(f'Rocket DSA/VTD fallback result: {retest_result}', 'info')
    _pause(f'Rocket DSA/VTD fallback retry {retest_result} — press any key to continue...')
    return retest_result


def run_memicals(s: SVOSSession) -> str:
    _status('Running Memicals (memic.py -M 15)...', 'step')
    s.send('memic.py -M 15 memicals:high-mem:proc -X proc:0,1,2,3 > memic_tst.txt')
    with _guard('Memicals - memic.py'):
        s.read_until(SVOS_PROMPT, timeout=MEMIC_TIMEOUT)
    s.send('grep -i "pass\\|fail\\|success" memic_tst.txt')
    with _guard('parse memic_tst.txt'):
        _, buf = s.read_until_any([SVOS_PROMPT], timeout=CMD_TIMEOUT)
    result = 'PASS' if (b'PASS' in buf.upper() or b'SUCCESS' in buf.upper()) else ('FAIL' if b'FAIL' in buf.upper() else 'UNKNOWN')
    _status(f'Memicals: {result}', 'ok' if result == 'PASS' else 'fail')
    _pause(f'Memicals {result} — press any key to continue...')
    return result


def run_mlc(s: SVOSSession) -> str:
    _status('Running MLC (mlc --loaded_latency -t60)...', 'step')
    s.send('mlc --loaded_latency -t60 -Mdatapattern_halfA_half5.txt > mlc_test.txt')
    with _guard('MLC'):
        s.read_until(SVOS_PROMPT, timeout=MLC_TIMEOUT)
    s.send('grep -i "pass\\|fail\\|success" mlc_test.txt')
    with _guard('parse mlc_test.txt'):
        _, buf = s.read_until_any([SVOS_PROMPT], timeout=CMD_TIMEOUT)
    result = 'PASS' if (b'PASS' in buf.upper() or b'SUCCESS' in buf.upper()) else ('FAIL' if b'FAIL' in buf.upper() else 'UNKNOWN')
    _status(f'MLC: {result}', 'ok' if result == 'PASS' else 'fail')
    _pause(f'MLC {result} — press any key to continue...')
    return result


def run_solar(s: SVOSSession) -> str:
    _status('Running Solar...', 'step')
    s.send(SOLAR_CMD)
    solar_timeout = False
    try:
        _, buf = s.read_until_any([b'PASS', b'pass', b'FAIL', b'fail',
                                    SVOS_PROMPT], timeout=SOLAR_TIMEOUT)
        result = 'PASS' if (b'PASS' in buf or b'pass' in buf) else 'FAIL'
    except TimeoutError:
        result = 'FAIL'
        solar_timeout = True
        _status('Solar: TIMEOUT', 'fail')
    try:
        s.read_until(SVOS_PROMPT, timeout=120)
    except TimeoutError:
        solar_timeout = True

    if solar_timeout:
        _recover_svos_prompt_after_timeout(s, 'Solar')

    _status(f'Solar: {result}', 'ok' if result == 'PASS' else 'fail')
    _pause(f'Solar {result} — press any key to continue...')
    return result


def run_parser(s: SVOSSession):
    """Replicates parser() from the bash script: grep PASS/fail in all .txt files."""
    _status('Running parser (grep on *.txt)...', 'step')
    try:
        s.send('grep -r "PASS" *.txt >> output.log 2>/dev/null; grep -r "success" *.txt >> output.log 2>/dev/null; grep -r "fail" *.txt >> output.log 2>/dev/null')
        with _guard('parser grep'):
            s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
        s.send('mkdir -p output && mv output.log output/ 2>/dev/null; cat output/output.log')
        with _guard('move output.log'):
            s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
        _status('Parser completed. Results in output/output.log', 'ok')
    except FCOStepError as e:
        if 'TIMEOUT waiting for:' in str(e):
            _recover_svos_prompt_after_timeout(s, 'Parser')
        raise


def run_svos_boot_check(s: SVOSSession) -> str:
    """Validates SVOS responsiveness by running svosinfo and waiting for prompt."""
    _status('Validating SVOS boot with svosinfo...', 'step')
    try:
        s.flush()
        s.send('svosinfo')
        with _guard('svosinfo response for SVOS boot check'):
            s.read_until(SVOS_PROMPT, timeout=SVOSINFO_TIMEOUT)
        _status('SVOS boot validation successful (svosinfo responded).', 'ok')
        return 'PASS'
    except Exception as e:
        _status(f'SVOS boot validation FAILED: {e}', 'fail')
        logging.error(f'SVOS boot validation failed: {e}', exc_info=True)
        return 'FAIL'


def get_ifwi_version() -> str:
    """Looks for the most recent .log file in C:\\DediLog and extracts the IFWI name (csPath=)."""
    dedi_dir = Path('C:/DediLog')
    if not dedi_dir.exists():
        return 'pending'
    logs = sorted(dedi_dir.glob('*.log'), key=lambda f: f.stat().st_mtime, reverse=True)
    if not logs:
        return 'pending'
    try:
        text = logs[0].read_text(encoding='utf-8', errors='ignore')
        for line in reversed(text.splitlines()):
            if 'csPath=' in line:
                raw_path = line.split('csPath=', 1)[1].strip()
                return raw_path
    except Exception as e:
        logging.warning(f'Could not read IFWI from DediLog: {e}')
    return 'pending'


def write_result_log(qdf: str, week: str, ult0: str, ifwi: str, results: dict,
                     log_dir: Path = REPORTS_DIR, timings: dict = None, content=None, vid: str = '',
                     ult_vid: str = ''):
    """Generates fco_result_{qdf}.txt in logs/reports/ and archives it there."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts      = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    def _base_result(v):
        if not isinstance(v, str):
            return v
        return v.replace(' (Retest)', '').strip()

    overall = 'PASS' if all(_base_result(v) == 'PASS'
                            for v in results.values()
                            if _base_result(v) != 'SKIPPED') else 'FAIL'

    # Column widths for the content table
    NAME_W = 20
    CMD_W  = 75

    # Output order: rockets grouped together (cpu, iax, dsa consecutive)
    OUTPUT_ORDER = [
        'supercollider',
        'rocket_dram_cpu',
        'rocket_dram_iax',
        'rocket_dram_dsa',
        'memicals',
        'solar',
        'mlc',
        'svos_boot',
        'centos_boot',
    ]

    def _add_cmd_lines(rows, name, cmd, status):
        """Adds one or more lines for an item, with command wrapping."""
        if len(cmd) <= CMD_W:
            rows.append(f'  {name:<{NAME_W}} {cmd:<{CMD_W}} : {status or "N/A"}')
        else:
            rows.append(f'  {name:<{NAME_W}} {cmd[:CMD_W]:<{CMD_W}} : {status or "N/A"}')
            rest = cmd[CMD_W:]
            indent = ' ' * (2 + NAME_W + 1)
            while rest:
                rows.append(f'{indent}{rest[:CMD_W]}')
                rest = rest[CMD_W:]

    def _content_rows():
        rows = []
        for name in OUTPUT_ORDER:
            if name not in results:
                continue
            cmd = CONTENT_CMDS.get(name, '')
            _add_cmd_lines(rows, name, cmd, results[name])
        return rows

    if content is None:
        selected_str = 'Full content'
    else:
        selected_str = ', '.join(_CONTENT_DISPLAY.get(k, k) for k in content)

    lines = [
        'FCO Content Result Log',
        '======================',
        f'Date/Time : {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'Week      : WW{week}',
        f'ULT/VID   : {ult_vid}' if ult_vid else f'ULT       : {ult0}',
        f'VID       : {vid}' if vid else None,
        f'QDF       : {qdf}',
        f'IFWI      : {ifwi}',
        f'Selected  : {selected_str}',
        f'Overall   : {overall}',
        '',
        f'  {"Content":<{NAME_W}} {"Command":<{CMD_W}}   Result',
        f'  {"-"*NAME_W} {"-"*CMD_W}   ------',
    ]
    lines = [ln for ln in lines if ln is not None] + _content_rows()

    content = '\n'.join(lines)

    if timings:
        TIMING_LABELS = {
            'overwrite_wait': 'Bootscript Excecution (Fuse Overwrite)',
            'boot':           'Boot SVOS',
            'supercollider':  'SuperCollider',
            'rocket_cpu_iax': 'Rocket cpu+iax',
            'memicals':       'Memicals',
            'solar':          'Solar',
            'mlc':            'MLC',
            'rocket_dsa':     'Rocket DSA',
            'svos_boot':      'SVOS Boot Check',
            'centos_boot':    'CentOS Boot',
        }
        LBL_W = 18
        DUR_W = 12
        t_rows = []
        total  = 0.0
        for key, label in TIMING_LABELS.items():
            if key in timings:
                secs = timings[key]
                total += secs
                t_rows.append(f'  {label:<{LBL_W}} {_fmt_dur(secs):>{DUR_W}}')
        t_rows.append(f'  {"-"*LBL_W} {"-"*DUR_W}')
        t_rows.append(f'  {"TOTAL":<{LBL_W}} {_fmt_dur(total):>{DUR_W}}')
        content += '\n' + '\n'.join(['', 'Timing', '------'] + t_rows)

    # Store the latest result and the timestamped archive in logs/reports.
    log_dir.mkdir(parents=True, exist_ok=True)
    local_path = log_dir / f'fco_result_{qdf}.txt'
    local_path.write_text(content, encoding='utf-8')

    archive_path = log_dir / f'FCO_WW{week}_{qdf}_{ts}.txt'
    archive_path.write_text(content, encoding='utf-8')

    logging.info(f'Log saved: {local_path}')
    return local_path, overall, content


def write_summary_log(week: str, ult0: str, ifwi: str, all_results: list, log_dir: Path = REPORTS_DIR,
                      vid: str = '', ult_vid: str = ''):
    """Generates FCO_SUMMARY_WW{week}.txt with the results for all QDFs in logs/reports/."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    all_pass = all(r['overall'] in ('PASS', 'RETRY_PASS') for r in all_results)
    overall  = 'PASS' if all_pass else 'FAIL'

    QDF_W = 12
    OV_W  = 16
    has_timing = any('timing_total' in r for r in all_results)
    DUR_W = 12

    rows = []
    for r in all_results:
        ov  = r['overall']
        log = r.get('log', '')
        if has_timing:
            dur = _fmt_dur(r['timing_total']) if 'timing_total' in r else 'N/A'
            rows.append(f'  {r["qdf"]:<{QDF_W}} {ov:<{OV_W}} {dur:<{DUR_W}} {log}')
        else:
            rows.append(f'  {r["qdf"]:<{QDF_W}} {ov:<{OV_W}} {log}')

    hdr_dur = f' {"Duration":<{DUR_W}}' if has_timing else ''
    sep_dur = f' {"-"*DUR_W}'           if has_timing else ''

    lines = [
        'FCO Execution Summary',
        '=====================',
        f'Date/Time : {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'Week      : WW{week}',
        f'ULT/VID   : {ult_vid}' if ult_vid else f'ULT       : {ult0}',
        f'VID       : {vid}' if vid else None,
        f'IFWI      : {ifwi}',
        f'Overall   : {overall}',
        f'QDFs      : {len(all_results)}  '
        f'(PASS: {sum(1 for r in all_results if r["overall"] in ("PASS","RETRY_PASS"))} / '
        f'FAIL: {sum(1 for r in all_results if r["overall"] not in ("PASS","RETRY_PASS"))})',
        '',
        f'  {"QDF":<{QDF_W}} {"Result":<{OV_W}}{hdr_dur} Log',
        f'  {"-"*QDF_W} {"-"*OV_W}{sep_dur} ---',
    ]
    lines = [ln for ln in lines if ln is not None] + rows

    content = '\n'.join(lines)

    # Embed the full log for each QDF at the end of the summary
    divider = '\n' + '=' * 70 + '\n'
    for r in all_results:
        rc = r.get('result_content')
        if rc:
            content += divider + rc

    log_dir.mkdir(parents=True, exist_ok=True)
    local_path   = log_dir / f'fco_summary_WW{week}.txt'
    archive_path = log_dir / f'FCO_SUMMARY_WW{week}_{ts}.txt'
    local_path.write_text(content,   encoding='utf-8')
    archive_path.write_text(content, encoding='utf-8')

    logging.info(f'Summary saved: {local_path}')
    return local_path





# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def wait_for_signal(sig_file: Path, poll=10):
    """Waits indefinitely until the signal arrives."""
    logging.info(f'Waiting for signal: {sig_file.name} ...')
    while not sig_file.exists():
        time.sleep(poll)
    logging.info(f'Signal received: {sig_file.name}')


def _clean_signals(qdf_list):
    """Removes .signal files from previous runs to avoid false positives."""
    for item in qdf_list:
        qdf = item['qdf']
        for name in [f'{qdf}_sv_done.signal', f'{qdf}_svos_done.signal',
                     f'{qdf}_retry_sv_done.signal', f'{qdf}_retry_svos_done.signal']:
            sig = SIGNAL_DIR / name
            if sig.exists():
                sig.unlink()
                logging.info(f'Previous signal removed: {name}')
    # Clean up global retry signals
    for name in ['retry_needed.signal', 'retry_needed.json', 'retry_ready.signal']:
        sig = SIGNAL_DIR / name
        if sig.exists():
            sig.unlink()
            logging.info(f'Previous signal removed: {name}')


def _self_update():
    """
    Pulls the latest version from GitHub before starting.
    Requires git to be installed and the local directory to be a git repo.
    If FCO_AutoTool.py was updated, relaunches the process with the same
    arguments so the new version is the one that runs.
    """
    import subprocess

    GIT_CANDIDATES = [
        r'C:\Program Files\Git\bin\git.exe',
        r'C:\Program Files (x86)\Git\bin\git.exe',
        'git',
    ]

    git_exe = None
    for candidate in GIT_CANDIDATES:
        try:
            subprocess.run([candidate, '--version'], capture_output=True, check=True)
            git_exe = candidate
            break
        except Exception:
            continue

    if git_exe is None:
        print('  [update] git not found, skipping update.')
        return

    # Verify this folder is a git repo
    result = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'rev-parse', '--is-inside-work-tree'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'  [update] {BASE_DIR} is not a git repo, skipping update.')
        return

    # Get current commit hash before pulling
    before = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'rev-parse', 'HEAD'],
        capture_output=True, text=True
    ).stdout.strip()

    # Clean local signals to guarantee a fresh run
    if SIGNAL_DIR.exists():
        removed = [f for f in SIGNAL_DIR.iterdir()
                   if f.is_file() and f.suffix in {'.signal', '.json'}]
        for f in removed:
            f.unlink()
        if removed:
            print(f'  [update] {len(removed)} previous signal(s) removed.')

    # README policy: allow overwrite from repo, but backup local edits first.
    readme_rel = 'README.txt'
    readme_path = BASE_DIR / readme_rel
    readme_status = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'status', '--porcelain', '--', readme_rel],
        capture_output=True, text=True
    )
    if readme_status.returncode == 0 and readme_status.stdout.strip() and readme_path.exists():
        try:
            backup_dir = LOG_DIR / 'update_backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f'README_before_update_{ts}.txt'
            backup_path.write_bytes(readme_path.read_bytes())
            print(f'  [update] README local backup saved: {backup_path}')
        except Exception as e:
            print(f'  [update] warning: could not backup README before overwrite: {e}')

        # Discard local README changes so pull can overwrite with repo version.
        restore_readme = subprocess.run(
            [git_exe, '-C', str(BASE_DIR), 'restore', '--', readme_rel],
            capture_output=True, text=True
        )
        if restore_readme.returncode != 0:
            print(f'  [update] warning: could not restore README before pull: {restore_readme.stderr.strip()}')

    # If there are local changes, stash them temporarily so pull can proceed.
    stash_created = False
    status = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'status', '--porcelain'],
        capture_output=True, text=True
    )
    if status.returncode == 0 and status.stdout.strip():
        print('  [update] Local changes detected. Creating temporary stash...')
        stash = subprocess.run(
            [git_exe, '-C', str(BASE_DIR), 'stash', 'push', '--include-untracked', '-m', 'fco-autoupdate-autostash'],
            capture_output=True, text=True
        )
        if stash.returncode != 0:
            print(f'  [update] could not stash local changes: {stash.stderr.strip()}')
            print('  [update] skipping git pull to avoid losing local work.')
            return
        stash_created = 'No local changes to save' not in (stash.stdout or '')

    # Pull latest changes from GitHub
    pull = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'pull', '--ff-only'],
        capture_output=True, text=True
    )

    if pull.returncode != 0:
        print(f'  [update] git pull failed: {pull.stderr.strip()}')
        if stash_created:
            pop = subprocess.run(
                [git_exe, '-C', str(BASE_DIR), 'stash', 'pop'],
                capture_output=True, text=True
            )
            if pop.returncode != 0:
                print('  [update] warning: could not auto-restore stash after pull failure.')
                print('           Recover manually with: git stash list / git stash pop')
        return

    if stash_created:
        print('  [update] Restoring local changes from temporary stash...')
        pop = subprocess.run(
            [git_exe, '-C', str(BASE_DIR), 'stash', 'pop'],
            capture_output=True, text=True
        )
        if pop.returncode != 0:
            print('  [update] warning: stash restore had conflicts. Resolve and continue.')

    # Get commit hash after pulling
    after = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'rev-parse', 'HEAD'],
        capture_output=True, text=True
    ).stdout.strip()

    if before == after:
        print('  [update] Everything up to date (GitHub).')
        return

    # List files changed between the two commits
    diff = subprocess.run(
        [git_exe, '-C', str(BASE_DIR), 'diff', '--name-only', before, after],
        capture_output=True, text=True
    ).stdout.strip()
    changed = diff.splitlines() if diff else []

    print(f'  [update] {len(changed)} file(s) updated: {", ".join(changed)}')

    # Fix local paths in README after update
    _update_readme()

    if 'FCO_AutoTool.py' in changed:
        print('  [update] FCO_AutoTool.py was updated — relaunching the new version...')
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)


def _update_readme():
    """Updates README path placeholders with the local BASE_DIR.
    To avoid dirtying tracked files every run, replacements happen only when
    explicit placeholders are present.
    Supported placeholders:
        __FCO_BASE_DIR__
        __FCO_BASE_DIR_RAW__
        <FCO_AutoTool_path>
    """
    readme = BASE_DIR / 'README.txt'
    if not readme.exists():
        return
    try:
        path_str = str(BASE_DIR)
        content  = readme.read_text(encoding='utf-8')

        updated = content
        updated = updated.replace('__FCO_BASE_DIR__', path_str)
        updated = updated.replace('__FCO_BASE_DIR_RAW__', path_str)
        # Backward compatibility with older README token.
        updated = updated.replace('<FCO_AutoTool_path>', path_str)

        if updated != content:
            # Replace only explicit placeholders; do not rewrite generic README lines.
            readme.write_text(updated, encoding='utf-8')
    except Exception:
        pass  # Not critical, do not interrupt execution

# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _ask_mode() -> int:
    """Shows the mode menu and returns the selected mode (1-4). Supports t prefix for TEST_MODE."""
    global TEST_MODE
    print()
    print('=' * 60)
    print('  FCO AUTOMATION — Execution mode')
    print('=' * 60)
    print()
    print('  1 - Unit NOT fused    (overwrite + test)         <- normal flow')
    print('  2 - Fused unit           (test fused QDF only)')
    print('  3 - Fused unit           (test fused + then overwrite other QDFs)')
    print('  4 - Fused unit           (ignore fused, overwrite other QDFs only)')
    print()
    print('  Prefix t for TEST MODE (e.g. t1, t2, t3, t4)')
    print()
    while True:
        raw = input('Mode (1-4): ').strip().lower()
        if raw in ('1', '2', '3', '4'):
            return int(raw)
        if raw in ('t1', 't2', 't3', 't4'):
            TEST_MODE = True
            print('  [TEST MODE activated]')
            return int(raw[1])
        print('  [!!] Enter 1, 2, 3 or 4.')


def _ask_com_week():
    """Prompts for COM port and week. Returns (com_port, week)."""
    print()
    com_port = input('COM port (e.g.: COM9 or just 9)                         : ').strip()

    if not com_port.upper().startswith('COM'):
        com_port = 'COM' + com_port

    week = input('Week number (e.g.: 17 or WW17)           : ').strip()
    week = week.upper().lstrip('W') if week.upper().startswith('WW') else week

    return com_port, week


def _ask_qdf_params(label=''):
    """Prompts for QDFs, ULT, SOC, kwargs. Returns (qdfs, ult0, soc, kwargs).
    label: additional text for the prompt, e.g. '(fused QDF)' or '(QDFs to overwrite)'"""
    label_str = f' {label}' if label else ''
    print()
    print(f'Enter the QDFs{label_str} separated by commas.')
    print('  Example: Q9WK, QABC, QXYZ')
    qdfs_raw = input(f'QDFs{label_str:<35}: ').strip()
    qdfs = [q.strip().upper() for q in qdfs_raw.split(',') if q.strip()]

    ult0 = input('ULT (same for all QDFs)            : ').strip()

    while True:
        soc = input('SOC (x4 / x1)                              : ').strip().lower()
        if soc in ('x4', 'x1'):
            break
        print('  [!!] Invalid option, enter x4 or x1.')

    kwargs = {}
    default_kwargs = _load_wrapper_default_kwargs()
    print()
    print('Default wrapper kwargs from config.json:')
    print(f'  {_format_kwargs_compact(default_kwargs)}')
    print('  Extra kwargs below are optional overrides/additions to those defaults.')
    print("Optional extra kwargs (e.g.: pwrgoodmethod='usb', fused_unit=False, extra_args={'disable_axon': True})")
    print("  Leave blank if not needed.")
    while True:
        raw_extra = input('Extra kwargs                               : ').strip()
        if not raw_extra:
            break
        try:
            import ast
            tree = ast.parse(f'_({raw_extra})', mode='eval')
            parsed = {}
            for kw in tree.body.keywords:
                parsed[kw.arg] = ast.literal_eval(kw.value)
            if not parsed:
                raise ValueError('No valid kwargs found')
            kwargs = parsed
            break
        except Exception:
            print("  [!!] Invalid format. Use Python kwargs format, e.g.: pwrgoodmethod='usb', fused_unit=False")

    return qdfs, ult0, soc, kwargs


def _ask_single_qdf(label='') -> str:
    """Prompts for a single QDF and returns it uppercase."""
    label_str = f' {label}' if label else ''
    while True:
        qdf = input(f'QDF{label_str:<36}: ').strip().upper()
        if qdf:
            return qdf
        print('  [!!] Enter a valid QDF.')


def _ask_ult(label='') -> str:
    """Prompts for ULT (single value)."""
    label_str = f' {label}' if label else ''
    return input(f'ULT{label_str:<36}: ').strip()


def _ask_skip_boot_if_in_svos() -> bool:
    """
    Asks whether to skip the initial SVOS boot when the user already has an
    active SVOS shell on the serial console.
    """
    print()
    print('Skip initial boot if already in SVOS shell? (root@... prompt)')
    resp = input('Skip boot (y/n): ').strip().lower()
    return resp in ('s', 'y', 'yes')


def _decide_skip_boot_from_serial(s: SVOSSession) -> bool:
    """
    Auto-detects whether SVOS is already active.
    Prompts for skip only when a live SVOS prompt is detected.
    """
    _status('Checking if SVOS prompt is active before skip-boot prompt...', 'info')
    if _is_svos_prompt_ready(s):
        _status('Active SVOS prompt detected.', 'ok')
        return _ask_skip_boot_if_in_svos()

    _status('SVOS prompt not detected. Continuing with normal boot flow (no skip prompt).', 'info')
    return False


def _is_svos_prompt_ready(s: SVOSSession, timeout: int = 4) -> bool:
    """Checks if serial is currently at an active SVOS prompt."""
    try:
        # Two ENTER round-trips reduce false positives from stale root@ text.
        s.flush()
        s.send_enter()
        s.read_until_any([SVOS_PROMPT], timeout=timeout)
        s.send_enter()
        s.read_until_any([SVOS_PROMPT], timeout=timeout)
        return True
    except Exception:
        return False


def _has_svos_content(content) -> bool:
    """
    Checks if there is any SVOS content to run (not just CentOS boot).
    Returns True if there's at least one test other than centos_boot/svos_boot.
    content=None means full content (True).
    """
    if content is None:
        return True
    return any(test in content for test in CONTENT_TESTS if test not in ('centos_boot', 'svos_boot'))


def run_centos_boot(s: SVOSSession, mode: int, qdf: str, ult0: str, soc: str = 'x4', 
                     kwargs: dict = None, is_retry: bool = False,
                     centos_only: bool = False) -> str:
    """
    Boots CentOS as part of the QDF content workflow.
    For modes with pysv overwrite (1/3/4): triggers overwrite + reboot + boot CentOS.
    For mode 2 (fused, with SVOS tests): requests power cycle from pysv + boots CentOS.
    For mode 2 (centos_only=True): sends soft reboot via serial, no pysv needed.

    Returns 'PASS' or 'FAIL'.
    """
    _status(f'Running CentOS boot (reboot + {CENTOS_GRUB_PATH} + login)...', 'step')
    
    try:
        # Reboot/overwrite preparation depends on mode and fused status
        if mode == 2 and centos_only:
            # CentOS-only: no SVOS, no pysv needed — go straight to BIOS/CentOS navigation.
            _status('CentOS-only: sending reboot and then navigating to CentOS...', 'info')
            try:
                s.send('reboot')
                time.sleep(1)
            except Exception as e:
                _status(f'Could not send reboot before CentOS flow: {e}. Continuing...', 'warn')
        elif mode == 2:
            # Mode 2 with SVOS tests: request hardware power cycle from pysv.
            _status('Mode 2: Requesting power cycle from pysv...', 'info')

            # Clean stale signals to avoid false-positive completion.
            for sig_name in [f'{qdf}_centos_power_cycle.signal', f'{qdf}_centos_power_cycled.signal']:
                sig = SIGNAL_DIR / sig_name
                if sig.exists():
                    sig.unlink()
            
            # Write power cycle signal
            power_cycle_signal = SIGNAL_DIR / f'{qdf}_centos_power_cycle.signal'
            power_cycle_signal.write_text('requested\n')
            _status(f'Sent signal: {power_cycle_signal.name}', 'info')
            
            # Wait for power cycle completion
            power_cycled_signal = SIGNAL_DIR / f'{qdf}_centos_power_cycled.signal'
            _status(f'Waiting for pysv power cycle completion...', 'wait')
            _wait_for_file(power_cycled_signal)
            
            done_content = power_cycled_signal.read_text().strip()
            if done_content == 'error':
                _status('pysv reported error during power cycle.', 'fail')
                return 'FAIL'
            
            _status('Power cycle completed by pysv.', 'ok')
        else:
            # Modes with pysv (1, 3, 4): request wrapper to run for CentOS overwrite
            _status('Modes 1/3/4: Requesting wrapper execution from pysv...', 'info')
            
            # Write wrapper request signal
            wrapper_signal = SIGNAL_DIR / f'{qdf}_centos_wrapper.signal'
            wrapper_signal.write_text(json.dumps({
                'qdf': qdf, 'ult0': ult0, 'soc': soc, 'kwargs': kwargs or {}
            }) + '\n')
            _status(f'Sent signal: {wrapper_signal.name}', 'info')
            
            # Wait for wrapper completion
            wrapper_done_signal = SIGNAL_DIR / f'{qdf}_centos_wrapper_done.signal'
            _status(f'Waiting for pysv wrapper execution...', 'wait')
            _wait_for_file(wrapper_done_signal)
            
            done_content = wrapper_done_signal.read_text().strip()
            if done_content == 'error':
                _status('pysv reported error in wrapper execution.', 'fail')
                return 'FAIL'
            
            _status('Wrapper execution completed by pysv.', 'ok')
        
        # Boot CentOS
        boot_centos(s, fused_nudge=(mode in (2, 3, 4)))
        _status('CentOS boot successful.', 'ok')
        _pause('CentOS boot OK — validate and press any key to continue...')
        return 'PASS'
        
    except Exception as e:
        _status(f'CentOS boot FAILED: {e}', 'fail')
        logging.error(f'CentOS boot failed for {qdf}: {e}', exc_info=True)
        
        # Interactive prompt: allow user to skip, retry, or abort
        print('\n' + '='*60)
        print('  CentOS Boot Failed — Choose Action')
        print('='*60)
        print(f'  Error: {e}')
        print()
        print('  Options:')
        print('    [s] Skip — continue to next QDF (mark as FAIL)')
        print('    [r] Retry — attempt CentOS boot again')
        print('    [a] Abort — exit tool completely')
        print()
        
        while True:
            choice = input('  Choice [s/r/a]: ').strip().lower()
            if choice in ('s', 'skip'):
                _status('Skipping to next QDF.', 'info')
                return 'FAIL'
            elif choice in ('r', 'retry'):
                _status('Retrying CentOS boot...', 'step')
                try:
                    boot_centos(s, fused_nudge=(mode in (2, 3, 4)))
                    _status('CentOS boot successful (retry).', 'ok')
                    _pause('CentOS boot OK — validate and press any key to continue...')
                    return 'PASS'
                except Exception as e_retry:
                    _status(f'Retry also FAILED: {e_retry}', 'fail')
                    logging.error(f'CentOS boot retry failed for {qdf}: {e_retry}', exc_info=True)
                    return 'FAIL'
            elif choice in ('a', 'abort'):
                _status('Aborting execution.', 'fail')
                sys.exit(1)
            else:
                print('  Invalid choice. Enter s, r, or a.')


# ---------------------------------------------------------------------------
# Content configuration per QDF
# ---------------------------------------------------------------------------

def _should_run(content, test_name):
    """Returns True if the test should run.

    Semantics:
    - content=None means full SVOS content (supercollider/rocket/memicals/solar/mlc)
      without centos_boot and without svos_boot.
    - svos_boot runs only when explicitly selected.
    """
    if content is None:
        return test_name in ('supercollider', 'rocket', 'memicals', 'solar', 'mlc')
    return content is None or test_name in content


def _ask_content_config(qdf_list):
    """
    Asks the user which content to run per QDF and updates item['content']:
      None       -> full content
      list[str]  -> only the tests in the list (keys from CONTENT_TESTS)

    Requires selecting at least one test per QDF when it is not full content.
    """
    _LABELS = [
        ('supercollider', 'SuperCollider'),
        ('rocket',        'Rocket (cpu/iax/dsa)'),
        ('memicals',      'Memicals'),
        ('solar',         'Solar'),
        ('mlc',           'MLC'),
    ]
    _FULL_SVOS = [k for k, _ in _LABELS]

    print()
    resp = input('Full SVOS content for ALL QDFs? (y/n): ').strip().lower()
    if resp in ('s', 'y'):
        run_centos_all = input('Run CentOS Boot Check for ALL QDFs? (y/n): ').strip().lower() in ('s', 'y')
        for item in qdf_list:
            item['content'] = list(_FULL_SVOS)
            if run_centos_all:
                item['content'].append('centos_boot')
        print()
        return

    print()
    for item in qdf_list:
        qdf = item['qdf']
        print(f'  --- {qdf} ---')
        resp_full = input(f'  Full SVOS content for {qdf}? (y/n): ').strip().lower()
        if resp_full in ('s', 'y'):
            selected = list(_FULL_SVOS)
            run_centos = input('    Run CentOS Boot Check? (y/n): ').strip().lower() in ('s', 'y')
            if run_centos:
                selected.append('centos_boot')
            item['content'] = selected
            selected_display = ', '.join(_CONTENT_DISPLAY.get(k, k) for k in selected)
            print(f'  {qdf}: {selected_display}')
            print()
            continue

        selected = []
        while not selected:
            selected = []
            for key, label in _LABELS:
                r = input(f'    Run {label}? (y/n): ').strip().lower()
                if r in ('s', 'y'):
                    selected.append(key)

            # Ask SVOS check only when all base SVOS tests are NO.
            if not selected:
                run_svos_check = input(
                    '    No SVOS content selected. Run SVOS Boot check? (y/n): '
                ).strip().lower() in ('s', 'y')
                if run_svos_check:
                    selected.append('svos_boot')

            # CentOS is asked separately.
            run_centos = input(
                '    Run CentOS Boot Check? (y/n): '
            ).strip().lower() in ('s', 'y')
            if run_centos:
                selected.append('centos_boot')

            if not selected:
                print(f'  [!!] Select at least one test for {qdf}.')

        item['content'] = selected
        selected_display = ', '.join(_CONTENT_DISPLAY.get(k, k) for k in selected)
        print(f'  {qdf}: {selected_display}')
        print()




# ---------------------------------------------------------------------------
# Last saved configuration
# ---------------------------------------------------------------------------

_MODE_DESC = {
    1: 'Unit NOT fused (overwrite + test)',
    2: 'Fused unit (test fused QDF only)',
    3: 'Fused unit (test fused + overwrite other QDFs)',
    4: 'Fused unit (ignore fused, overwrite only)',
}


def _save_last_config(cfg: dict):
    """Saves the current configuration to last_config.json for quick reuse."""
    try:
        with open(LAST_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _status(f'Could not save the last configuration: {e}', 'warn')


def _load_last_config() -> dict:
    """Loads the saved configuration. Returns None if it does not exist or there is an error."""
    if not LAST_CONFIG_FILE.exists():
        return None
    try:
        with open(LAST_CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_json_file(path: Path, data: dict):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _status(f'Could not save {path.name}: {e}', 'warn')


def _load_json_file(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _load_wrapper_default_kwargs() -> dict:
    """Loads default wrapper kwargs from config.json for user display."""
    data = _load_json_file(WRAPPER_CONFIG_FILE)
    if not isinstance(data, dict):
        return {
            'proj': 'DMRUCC',
            'stepping': 'a0',
            'pwrgoodmethod': 'usb',
            'fused_unit': False,
            'pwrgooddelay': 30,
            'extra_args': {'disable_axon': True},
        }
    return {k: v for k, v in data.items() if not str(k).startswith('_')}


def _format_kwargs_compact(kwargs: dict) -> str:
    return ', '.join(f'{k}={v!r}' for k, v in kwargs.items())


def _print_last_config(cfg: dict):
    """Displays a readable summary of the last saved configuration."""
    print()
    print('=' * 60)
    print('  Last saved configuration:')
    print('=' * 60)
    mode = cfg.get('mode', '?')
    print(f'  Mode:      {mode} — {_MODE_DESC.get(mode, "?")}')
    if cfg.get('test_mode'):
        print(f'  TEST MODE: active')
    print(f'  COM:       {cfg.get("com_port", "?")}')
    print(f'  Week:    WW{cfg.get("week", "?")}')
    print(f'  CentOS:    {"enabled" if cfg.get("boot_centos_after_fco") else "disabled"}')

    qdf_list = cfg.get('qdf_list') or []
    if qdf_list:
        print(f'  QDFs:      {", ".join(i["qdf"] for i in qdf_list)}')
        print(f'  ULT:       {qdf_list[0].get("ult0", "?")}')
        print(f'  SOC:       {qdf_list[0].get("soc", "?")}')
        if qdf_list[0].get('kwargs') or qdf_list[0].get('extra_args'):
            kw = qdf_list[0].get('kwargs') or {'extra_args': qdf_list[0]['extra_args']}
            print(f'  Extra:     {kw}')
        contents = []
        for i in qdf_list:
            c = i.get('content')
            contents.append(f'{i["qdf"]}: {"full" if c is None else ", ".join(c)}')
        print(f'  Content:   {" | ".join(contents)}')

    fused_qdf = cfg.get('fused_qdf')
    if fused_qdf:
        fused_ult_vid = cfg.get('fused_ult_vid', '')
        fused_vid = cfg.get('fused_vid', '')
        if fused_ult_vid:
            id_str = f'  ULT/VID={fused_ult_vid}'
        elif cfg.get('fused_ult0') not in (None, ''):
            id_str = f'  ULT={cfg.get("fused_ult0")}'
        elif fused_vid:
            id_str = f'  VID={fused_vid}'
        else:
            id_str = ''
        print(f'  Fused QDF:  {fused_qdf}{id_str}  SOC={cfg.get("fused_soc")}')
        fc = cfg.get('fused_content')
        print(f'  Content:      {"full" if fc is None else ", ".join(fc)}')

    ow_list = cfg.get('overwrite_qdf_list') or []
    if ow_list:
        print(f'  QDFs overwrite: {", ".join(i["qdf"] for i in ow_list)}')
        if ow_list[0].get('ult0'):
            print(f'  ULT overwrite:  {ow_list[0]["ult0"]}')
            ow_contents = []
            for i in ow_list:
                c = i.get('content')
                ow_contents.append(f'{i["qdf"]}: {"full" if c is None else ", ".join(c)}')
            print(f'  Content:        {" | ".join(ow_contents)}')

    print('=' * 60)


def _open_serial(com_port: str) -> 'SVOSSession':
    """
    Opens the serial port. If it fails because of PermissionError (e.g. TeraTerm is open),
    shows the error and offers to retry instead of exiting immediately.
    """
    while True:
        try:
            s = SVOSSession(com_port)
            logging.info(f'Port {com_port} opened at {BAUDRATE} baud.')
            # Clear stale serial/driver leftovers so a new run starts from a clean buffer.
            try:
                s.ser.reset_input_buffer()
                s.ser.reset_output_buffer()
                time.sleep(0.2)
                s.flush()
                logging.info(f'Initial serial buffer clear completed on {com_port}.')
            except Exception as e:
                logging.warning(f'Initial serial buffer clear warning on {com_port}: {e}')
            return s
        except serial.SerialException as e:
            logging.error(f'Could not open {com_port}: {e}')
            print(f'\n[!!] Could not open {com_port}: {e}')
            if 'PermissionError' in str(e) or 'Access is denied' in str(e):
                print('     Tip: close TeraTerm or another program using the port.')
            resp = input('     Retry? (y/n): ').strip().lower()
            if resp not in ('s', 'y'):
                sys.exit(1)
            print(f'     Retrying {com_port}...')



    """Runs the standalone SVOS flow for a fused QDF (without SV signals).
    Returns (log_path, overall, results)."""
    t0_boot = time.time()
    boot_svos(s, fused_nudge=True)
    setup_fco_dir(s, qdf, week)
    if CRONOS_MODE:
        _timings.setdefault(qdf, {})['boot'] = time.time() - t0_boot

    results = {}

    def _run_safe(name, fn, *args, _tkey=None):
        """Runs a test, catches any failure, and reports it as FAIL."""
        t0 = time.time()
        try:
            result = fn(*args)
        except Exception as e:
            _status(f'{name} FAILED: {e}', 'fail')
            logging.error(f'{name} failed for {qdf}: {e}', exc_info=True)
            if isinstance(e, FCOStepError) and 'TIMEOUT waiting for:' in str(e):
                _recover_svos_prompt_after_timeout(s, name)
            result = 'FAIL'
        if CRONOS_MODE and _tkey:
            _timings.setdefault(qdf, {})[_tkey] = time.time() - t0
        return result

    if _should_run(content, 'supercollider'):
        results['supercollider'] = _run_safe('SuperCollider', run_supercollider, s,
                                             _tkey='supercollider')
    else:
        results['supercollider'] = 'SKIPPED'

    if _should_run(content, 'rocket'):
        rocket_res = _run_safe('Rocket cpu/dsa-vtd/iax', run_rocket, s, _tkey='rocket_cpu_iax')
        if isinstance(rocket_res, dict):
            results.update(rocket_res)
        else:
            for _, label in ROCKET_CMDS:
                results[label] = 'FAIL'
            results['rocket_dram_dsa'] = 'FAIL'
    else:
        for _, label in ROCKET_CMDS:
            results[label] = 'SKIPPED'
        results['rocket_dram_dsa'] = 'SKIPPED'

    results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals')
                           if _should_run(content, 'memicals') else 'SKIPPED')
    results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar')
                           if _should_run(content, 'solar')    else 'SKIPPED')
    results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc')
                           if _should_run(content, 'mlc')      else 'SKIPPED')

    if _should_run(content, 'rocket'):
        dsa_fast = results.get('rocket_dram_dsa', 'FAIL')
        if dsa_fast != 'PASS':
            results['rocket_dram_dsa'] = _run_safe(
                'Rocket DSA fallback',
                run_rocket_dsa,
                s,
                dsa_fast,
                _tkey='rocket_dsa'
            )

    try:
        run_parser(s)
    except Exception as e:
        _status(f'Parser failed: {e}', 'fail')

    timings = _timings.get(qdf) if CRONOS_MODE else None
    log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, REPORTS_DIR,
                                                         timings=timings, content=content)

    if overall == 'FAIL':
        _status(f'Fused QDF {qdf}: FAILED — see {log_path}', 'fail')
        _alert_popup_async(f'FCO FAILED — {qdf}',
                           f'One or more content items failed.\nSee: {log_path}')
    else:
        _status(f'Fused QDF {qdf}: PASS', 'ok')

    return log_path, overall, result_content


# ---------------------------------------------------------------------------
# Main loop (modes 1, 3-phase-B and 4): SV signals + SVOS boot + tests
# ---------------------------------------------------------------------------

def _run_main_loop(s: SVOSSession, qdf_list: list, week: str, ult0: str, ifwi: str,
                   mode: int, skip_initial_boot: bool = False) -> list:
    """Executes the SV signals + SVOS boot + tests loop with retry. Returns all_results."""
    all_results  = []
    retry_needed = []

    _status('If pysv helper is not running yet, execute the commands below in your pysv console.', 'info')
    _print_pysv_overwrite_instructions()

    skip_boot_once = skip_initial_boot

    for i, item in enumerate(qdf_list):
        qdf  = item['qdf']
        ult0 = item['ult0']

        print(f'\n{"="*60}')
        _status(f'QDF {i+1}/{len(qdf_list)}: {qdf}', 'step')
        print(f'{"="*60}')

        try:
            sv_started = SIGNAL_DIR / f'{qdf}_sv_started.signal'
            sv_done = SIGNAL_DIR / f'{qdf}_sv_done.signal'
            _status(f'Waiting for SV to start/complete the fuse overwrite of {qdf}...', 'wait')

            first_signal = _wait_for_any_file([sv_started, sv_done], poll=1)
            if first_signal == sv_started:
                _status(f'Overwrite of {qdf} started by sv_automation.', 'info')
                t0_ow = time.time()
                with _monitor_stage(f'{qdf} - Bootscript Excecution (Fuse Overwrite)'):
                    _wait_for_file(sv_done, poll=1)
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['overwrite_wait'] = time.time() - t0_ow
            else:
                _status(f'Overwrite of {qdf} completed without start signal (legacy sv_automation).', 'info')

            content_sv_done = sv_done.read_text(encoding='utf-8').strip()
            if content_sv_done == 'error':
                raise FCOStepError(f'sv_automation reported an error during overwrite of {qdf}.')

            _status(f'Fuse overwrite of {qdf} completed.', 'ok')

            # Clean CentOS power cycle signals from previous run (if any)
            for sig_name in [f'{qdf}_centos_power_cycle.signal', f'{qdf}_centos_power_cycled.signal',
                            f'{qdf}_centos_wrapper.signal', f'{qdf}_centos_wrapper_done.signal']:
                sig = SIGNAL_DIR / sig_name
                if sig.exists():
                    sig.unlink()

            content = item.get('content')
            has_svos_tests = _has_svos_content(content)
            # Skip SVOS boot when only CentOS is selected.
            needs_svos = content is None or any(
                t in content for t in CONTENT_TESTS if t != 'centos_boot'
            )

            t0_boot = time.time()
            with _monitor_stage(f'{qdf} - Boot SVOS and setup'):
                if needs_svos:
                    if skip_boot_once:
                        _status('Skip boot enabled: validating current SVOS prompt...', 'info')
                        if _is_svos_prompt_ready(s):
                            _status('SVOS prompt detected. Continuing without boot.', 'ok')
                        else:
                            _status('SVOS prompt not detected. Falling back to normal boot flow.', 'warn')
                            boot_svos(s, fused_nudge=(mode == 4))
                        skip_boot_once = False
                    else:
                        boot_svos(s, fused_nudge=(mode == 4))
                    if has_svos_tests:
                        setup_fco_dir(s, qdf, week)
                else:
                    _status('CentOS-only content: skipping SVOS boot.', 'info')

            if CRONOS_MODE:
                _timings.setdefault(qdf, {})['boot'] = time.time() - t0_boot

            results = {}

            def _run_safe(name, fn, *args, _tkey=None, _monitor_label=None):
                """Runs a test, catches any failure, and reports it as FAIL."""
                t0 = time.time()
                try:
                    if _monitor_label:
                        with _monitor_stage(_monitor_label):
                            result = fn(*args)
                    else:
                        result = fn(*args)
                except Exception as e:
                    _status(f'{name} FAILED: {e}', 'fail')
                    logging.error(f'{name} failed for {qdf}: {e}', exc_info=True)
                    if isinstance(e, FCOStepError) and 'TIMEOUT waiting for:' in str(e):
                        _recover_svos_prompt_after_timeout(s, name)
                    result = 'FAIL'
                if CRONOS_MODE and _tkey:
                    _timings.setdefault(qdf, {})[_tkey] = time.time() - t0
                return result

            if _should_run(content, 'supercollider'):
                results['supercollider'] = _run_safe('SuperCollider', run_supercollider, s,
                                                     _tkey='supercollider',
                                                     _monitor_label=f'{qdf} - SuperCollider')
            else:
                results['supercollider'] = 'SKIPPED'

            if _should_run(content, 'rocket'):
                rocket_res = _run_safe('Rocket suite', run_rocket, s,
                                       _tkey='rocket_cpu_iax',
                                       _monitor_label=f'{qdf} - Rocket suite')
                if isinstance(rocket_res, dict):
                    results.update(rocket_res)
                else:
                    for _, label in ROCKET_CMDS:
                        results[label] = 'FAIL'
                    results['rocket_dram_dsa'] = 'FAIL'
            else:
                for _, label in ROCKET_CMDS:
                    results[label] = 'SKIPPED'
                results['rocket_dram_dsa'] = 'SKIPPED'

            results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals',
                                             _monitor_label=f'{qdf} - Memicals')
                                   if _should_run(content, 'memicals') else 'SKIPPED')
            results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar',
                                             _monitor_label=f'{qdf} - Solar')
                                   if _should_run(content, 'solar')    else 'SKIPPED')
            results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc',
                                             _monitor_label=f'{qdf} - MLC')
                                   if _should_run(content, 'mlc')      else 'SKIPPED')

            if _should_run(content, 'svos_boot'):
                t0_svos_boot = time.time()
                with _monitor_stage(f'{qdf} - SVOS boot check'):
                    results['svos_boot'] = run_svos_boot_check(s)
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['svos_boot'] = time.time() - t0_svos_boot
            else:
                # If any SVOS content already ran successfully, SVOS boot is implicitly validated.
                results['svos_boot'] = 'PASS' if has_svos_tests else 'SKIPPED'

            if _should_run(content, 'rocket'):
                dsa_fast = results.get('rocket_dram_dsa', 'FAIL')
                if dsa_fast != 'PASS':
                    results['rocket_dram_dsa'] = _run_safe(
                        'Rocket DSA fallback',
                        run_rocket_dsa,
                        s,
                        dsa_fast,
                        _tkey='rocket_dsa',
                        _monitor_label=f'{qdf} - Rocket DSA fallback'
                    )

            if has_svos_tests:
                try:
                    run_parser(s)
                except Exception as e:
                    _status(f'Parser failed: {e}', 'fail')

            if _should_run(content, 'centos_boot'):
                t0_centos = time.time()
                with _monitor_stage(f'{qdf} - Boot CentOS'):
                    centos_result = run_centos_boot(s, mode, qdf, ult0, item.get('soc', 'x4'),
                                                    item.get('kwargs'), is_retry=False)
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['centos_boot'] = time.time() - t0_centos
                results['centos_boot'] = centos_result
            else:
                results['centos_boot'] = 'SKIPPED'

            log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, REPORTS_DIR,
                                                  timings=_timings.get(qdf) if CRONOS_MODE else None,
                                                  content=content)

            # Only write result to SVOS if we didn't boot CentOS (system is still in SVOS)
            if results.get('centos_boot') not in ('PASS', 'FAIL'):
                # CentOS was skipped or not run — we're still in SVOS, safe to write result
                try:
                    summary_lines = [f'FCO WW{week} {qdf} - Overall: {overall}'] + \
                                    [f'{k}: {v}' for k, v in results.items()]
                    s.send(f'echo "{chr(10).join(summary_lines)}" > fco_result.txt')
                    s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
                except Exception as e:
                    _status(f'Could not write fco_result.txt in SVOS: {e}', 'fail')
            else:
                # CentOS was booted successfully (PASS or FAIL) — system is in CentOS, not SVOS
                _status(f'CentOS booted — result saved locally only (not writing to SVOS)', 'info')

            if overall == 'FAIL':
                _status(f'QDF {qdf}: FAILED - see {log_path}', 'fail')
                _alert_popup_async(f'FCO FAILED — {qdf}',
                                   f'One or more content items failed.\nSee: {log_path}')
            else:
                _status(f'QDF {qdf}: PASS', 'ok')

            timing_total = sum(_timings.get(qdf, {}).values()) if CRONOS_MODE else None
            entry = {'qdf': qdf, 'overall': overall, 'log': str(log_path),
                     'result_content': result_content}
            if timing_total is not None:
                entry['timing_total'] = timing_total
            all_results.append(entry)

        except (MountsvTimeoutError, BiosTimeoutError) as e:
            reason = type(e).__name__
            _status(f'QDF {qdf}: {reason} — will be added to the retry queue', 'fail')
            logging.error(f'{reason} in QDF {qdf}: {e}')
            _alert_popup_async(f'{reason} — {qdf}',
                               f'It will be retried at the end with a power cycle.\n{e}')
            all_results.append({'qdf': qdf, 'overall': reason, 'log': str(e)})
            retry_needed.append({'qdf': qdf, 'ult0': ult0,
                                 'soc': item.get('soc', 'x4'),
                                 'kwargs': item.get('kwargs'),
                                 'content': item.get('content'),
                                 'reason': reason})

        except Exception as e:
            _status(f'ERROR in QDF {qdf}: {e}', 'fail')
            logging.error(f'Unexpected error in QDF {qdf}: {e}', exc_info=True)
            _alert_popup_async(f'ERROR in QDF {qdf}', str(e))
            all_results.append({'qdf': qdf, 'overall': 'ERROR', 'log': str(e)})
            _status('Continuing with the next QDF...', 'step')

        finally:
            svos_done = SIGNAL_DIR / f'{qdf}_svos_done.signal'
            try:
                # Only write svos_done if the QDF completed SUCCESSFULLY
                # This signals to pysv (idle loop) that this QDF is ready to move to the next
                if all_results and all_results[-1]['overall'] == 'PASS':
                    svos_done.write_text('done\n')
                    _status(f'Signal written for SV: {svos_done.name}', 'info')
                else:
                    # If failed/error/timeout, write error signal so pysv knows about the failure
                    svos_done.write_text('error\n')
                    _status(f'Error signal written for SV: {svos_done.name}', 'info')
            except Exception as e:
                _status(f'Could not write the SV signal: {e}', 'fail')

    # ---- PHASE 2: Retry QDFs with BIOS/MOUNTSV timeout ----
    if retry_needed:
        print(f'\n{"="*60}')
        _status(f'PHASE 2 — Retry of {len(retry_needed)} QDF(s): '
                f'{[r["qdf"] for r in retry_needed]}', 'step')
        print(f'{"="*60}')

        retry_json = SIGNAL_DIR / 'retry_needed.json'
        retry_json.write_text(json.dumps(retry_needed, indent=2), encoding='utf-8')
        (SIGNAL_DIR / 'retry_needed.signal').write_text('retry\n')
        _status('retry_needed signal sent to SV. Waiting for power cycle...', 'wait')
        wait_for_signal(SIGNAL_DIR / 'retry_ready.signal')
        _status('Power cycle completed. Starting retry...', 'ok')

        for retry_item in retry_needed:
            qdf  = retry_item['qdf']
            ult0 = retry_item['ult0']
            print(f'\n{"="*60}')
            _status(f'RETRY — QDF {qdf} (reason: {retry_item["reason"]})', 'step')
            print(f'{"="*60}')

            try:
                with _monitor_stage(f'{qdf} - Retry Bootscript Excecution (Fuse Overwrite)'):
                    wait_for_signal(SIGNAL_DIR / f'{qdf}_retry_sv_done.signal')
                _status(f'Retry overwrite of {qdf} completed.', 'ok')

                content_r = retry_item.get('content')
                has_svos_tests_r = _has_svos_content(content_r)
                needs_svos_r = content_r is None or any(
                    t in content_r for t in CONTENT_TESTS if t != 'centos_boot'
                )
                t0_boot_r = time.time()
                with _monitor_stage(f'{qdf} - Retry Boot SVOS and setup'):
                    if needs_svos_r:
                        boot_svos(s, fused_nudge=(mode == 4))
                        if has_svos_tests_r:
                            setup_fco_dir(s, qdf, week)
                    else:
                        _status('CentOS-only retry: skipping SVOS boot.', 'info')
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['boot'] = time.time() - t0_boot_r

                results = {}

                def _run_safe_r(name, fn, *args, _tkey=None, _monitor_label=None):
                    t0 = time.time()
                    try:
                        if _monitor_label:
                            with _monitor_stage(_monitor_label):
                                result = fn(*args)
                        else:
                            result = fn(*args)
                    except Exception as e:
                        _status(f'{name} FAILED: {e}', 'fail')
                        logging.error(f'[RETRY] {name} failed for {qdf}: {e}', exc_info=True)
                        if isinstance(e, FCOStepError) and 'TIMEOUT waiting for:' in str(e):
                            _recover_svos_prompt_after_timeout(s, f'{name} (retry)')
                        result = 'FAIL'
                    if CRONOS_MODE and _tkey:
                        _timings.setdefault(qdf, {})[_tkey] = time.time() - t0
                    return result

                if _should_run(content_r, 'supercollider'):
                    results['supercollider'] = _run_safe_r('SuperCollider', run_supercollider, s,
                                                           _tkey='supercollider',
                                                           _monitor_label=f'{qdf} - Retry SuperCollider')
                else:
                    results['supercollider'] = 'SKIPPED'

                if _should_run(content_r, 'rocket'):
                    rocket_res = _run_safe_r('Rocket cpu/dsa-vtd/iax', run_rocket, s,
                                             _tkey='rocket_cpu_iax',
                                             _monitor_label=f'{qdf} - Retry Rocket cpu/dsa-vtd/iax')
                    if isinstance(rocket_res, dict):
                        results.update(rocket_res)
                    else:
                        for _, label in ROCKET_CMDS:
                            results[label] = 'FAIL'
                else:
                    for _, label in ROCKET_CMDS:
                        results[label] = 'SKIPPED'

                results['memicals'] = (_run_safe_r('Memicals', run_memicals, s, _tkey='memicals',
                                                   _monitor_label=f'{qdf} - Retry Memicals')
                                       if _should_run(content_r, 'memicals') else 'SKIPPED')
                results['solar']    = (_run_safe_r('Solar', run_solar, s, _tkey='solar',
                                                   _monitor_label=f'{qdf} - Retry Solar')
                                       if _should_run(content_r, 'solar')    else 'SKIPPED')
                results['mlc']      = (_run_safe_r('MLC', run_mlc, s, _tkey='mlc',
                                                   _monitor_label=f'{qdf} - Retry MLC')
                                       if _should_run(content_r, 'mlc')      else 'SKIPPED')

                if _should_run(content_r, 'svos_boot'):
                    t0_svos_boot_r = time.time()
                    with _monitor_stage(f'{qdf} - Retry SVOS boot check'):
                        results['svos_boot'] = run_svos_boot_check(s)
                    if CRONOS_MODE:
                        _timings.setdefault(qdf, {})['svos_boot'] = time.time() - t0_svos_boot_r
                else:
                    # Retry follows same rule: any executed SVOS content implies boot validation.
                    results['svos_boot'] = 'PASS' if has_svos_tests_r else 'SKIPPED'

                if _should_run(content_r, 'rocket'):
                    dsa_fast = results.get('rocket_dram_dsa', 'FAIL')
                    if dsa_fast != 'PASS':
                        results['rocket_dram_dsa'] = _run_safe_r(
                            'Rocket DSA fallback',
                            run_rocket_dsa,
                            s,
                            dsa_fast,
                            _tkey='rocket_dsa',
                            _monitor_label=f'{qdf} - Retry Rocket DSA fallback'
                        )
                else:
                    results['rocket_dram_dsa'] = 'SKIPPED'

                if has_svos_tests_r:
                    try:
                        run_parser(s)
                    except Exception as e:
                        _status(f'Parser failed in retry: {e}', 'fail')

                log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, REPORTS_DIR,
                                                     timings=_timings.get(qdf) if CRONOS_MODE else None,
                                                     content=content_r)
                retry_overall = f'RETRY_{overall}'
                _status(f'RETRY QDF {qdf}: {retry_overall}', 'ok' if overall == 'PASS' else 'fail')
                if overall == 'FAIL':
                    _alert_popup_async(f'RETRY FAILED — {qdf}',
                                       f'Retry completed but content items failed.\nSee: {log_path}')

                timing_total_r = sum(_timings.get(qdf, {}).values()) if CRONOS_MODE else None
                for r in all_results:
                    if r['qdf'] == qdf:
                        r['overall'] = retry_overall
                        r['log'] = str(log_path)
                        r['result_content'] = result_content
                        if timing_total_r is not None:
                            r['timing_total'] = timing_total_r
                        break

            except (MountsvTimeoutError, BiosTimeoutError) as e:
                _status(f'RETRY QDF {qdf} failed again ({type(e).__name__}): {e}', 'fail')
                logging.error(f'[RETRY] {type(e).__name__} in QDF {qdf}: {e}')
                _alert_popup_async(f'RETRY FAIL — {qdf}', f'Fallo en segundo intento.\n{e}')
                for r in all_results:
                    if r['qdf'] == qdf:
                        r['overall'] = 'RETRY_FAIL'
                        break

            except Exception as e:
                _status(f'RETRY QDF {qdf} unexpected error: {e}', 'fail')
                logging.error(f'[RETRY] Error in QDF {qdf}: {e}', exc_info=True)
                _alert_popup_async(f'RETRY ERROR — {qdf}', str(e))
                for r in all_results:
                    if r['qdf'] == qdf:
                        r['overall'] = 'RETRY_FAIL'
                        break

            finally:
                try:
                    (SIGNAL_DIR / f'{qdf}_retry_svos_done.signal').write_text('done\n')
                    _status(f'retry_svos_done signal written for {qdf}', 'info')
                except Exception as e:
                    _status(f'Could not write the retry signal: {e}', 'fail')

    return all_results


def run_fused_test(s: SVOSSession, qdf: str, ult0: str, week: str, ifwi: str,
                   content=None, mode=2, soc='x4', kwargs=None, vid: str = '',
                   ult_vid: str = '', skip_boot: bool = False) -> tuple:
    """
    Runs tests on a fused unit (Modes 2 & 3, Phase A).
    No signal coordination with SV — the unit is already fused.
    Returns: (log_path, overall, result_content)
    """
    # Only boot SVOS if there is at least one SVOS test (or svos_boot) selected.
    # If the user chose CentOS-only, skip SVOS entirely and go straight to power cycle.
    needs_svos = content is None or any(
        t in content for t in CONTENT_TESTS if t != 'centos_boot'
    )
    has_svos_tests = _has_svos_content(content)

    with _monitor_stage(f'{qdf} - Boot SVOS and setup'):
        if needs_svos:
            if skip_boot:
                _status('Skip boot enabled: validating current SVOS prompt...', 'info')
                if _is_svos_prompt_ready(s):
                    _status('SVOS prompt detected. Continuing without boot.', 'ok')
                else:
                    _status('SVOS prompt not detected. Falling back to normal boot flow.', 'warn')
                    boot_svos(s, fused_nudge=(mode in (2, 3)))
            else:
                boot_svos(s, fused_nudge=(mode in (2, 3)))
            if has_svos_tests:
                setup_fco_dir(s, qdf, week)
        else:
            _status('CentOS-only content: skipping SVOS boot.', 'info')

    results = {}

    def _run_safe(name, fn, *args, _tkey=None, _monitor_label=None):
        t0 = time.time()
        try:
            if _monitor_label:
                with _monitor_stage(_monitor_label):
                    result = fn(*args)
            else:
                result = fn(*args)
        except Exception as e:
            _status(f'{name} FAILED: {e}', 'fail')
            logging.error(f'{name} failed for fused QDF {qdf}: {e}', exc_info=True)
            if isinstance(e, FCOStepError) and 'TIMEOUT waiting for:' in str(e):
                _recover_svos_prompt_after_timeout(s, name)
            result = 'FAIL'
        if CRONOS_MODE and _tkey:
            _timings.setdefault(qdf, {})[_tkey] = time.time() - t0
        return result

    if _should_run(content, 'supercollider'):
        results['supercollider'] = _run_safe('SuperCollider', run_supercollider, s,
                                             _tkey='supercollider',
                                             _monitor_label=f'{qdf} - SuperCollider')
    else:
        results['supercollider'] = 'SKIPPED'

    if _should_run(content, 'rocket'):
        rocket_res = _run_safe('Rocket cpu/dsa-vtd/iax', run_rocket, s,
                               _tkey='rocket_cpu_iax',
                               _monitor_label=f'{qdf} - Rocket cpu/dsa-vtd/iax')
        if isinstance(rocket_res, dict):
            results.update(rocket_res)
        else:
            for _, label in ROCKET_CMDS:
                results[label] = 'FAIL'
    else:
        for _, label in ROCKET_CMDS:
            results[label] = 'SKIPPED'

    results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals',
                                     _monitor_label=f'{qdf} - Memicals')
                           if _should_run(content, 'memicals') else 'SKIPPED')
    results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar',
                                     _monitor_label=f'{qdf} - Solar')
                           if _should_run(content, 'solar')    else 'SKIPPED')
    results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc',
                                     _monitor_label=f'{qdf} - MLC')
                           if _should_run(content, 'mlc')      else 'SKIPPED')

    if _should_run(content, 'svos_boot'):
        t0_svos_boot = time.time()
        with _monitor_stage(f'{qdf} - SVOS boot check'):
            results['svos_boot'] = run_svos_boot_check(s)
        if CRONOS_MODE:
            _timings.setdefault(qdf, {})['svos_boot'] = time.time() - t0_svos_boot
    else:
        # Same rule as main loop: if any SVOS content ran, boot is implicitly validated.
        results['svos_boot'] = 'PASS' if has_svos_tests else 'SKIPPED'

    if _should_run(content, 'rocket'):
        dsa_fast = results.get('rocket_dram_dsa', 'FAIL')
        if dsa_fast != 'PASS':
            results['rocket_dram_dsa'] = _run_safe(
                'Rocket DSA fallback',
                run_rocket_dsa,
                s,
                dsa_fast,
                _tkey='rocket_dsa',
                _monitor_label=f'{qdf} - Rocket DSA fallback'
            )
    else:
        results['rocket_dram_dsa'] = 'SKIPPED'

    if has_svos_tests:
        try:
            run_parser(s)
        except Exception as e:
            _status(f'Parser failed: {e}', 'fail')

    if _should_run(content, 'centos_boot'):
        if mode == 2 and needs_svos:
            # SVOS tests ran: let pysv monitor know SVOS phase is complete
            # so it can do the hardware power cycle before CentOS.
            svos_done_signal = SIGNAL_DIR / f'{qdf}_svos_done.signal'
            svos_done_signal.write_text('done\n')
            _status(f'Mode 2: signal written for pysv monitor: {svos_done_signal.name}', 'info')
        t0_centos = time.time()
        # centos_only=True means skip pysv entirely and do a soft reboot from SVOS.
        with _monitor_stage(f'{qdf} - Boot CentOS'):
            centos_result = run_centos_boot(s, mode, qdf, ult0, soc, kwargs, is_retry=False,
                                            centos_only=(mode == 2 and not needs_svos))
        if CRONOS_MODE:
            _timings.setdefault(qdf, {})['centos_boot'] = time.time() - t0_centos
        results['centos_boot'] = centos_result
    else:
        results['centos_boot'] = 'SKIPPED'

    log_path, overall, result_content = write_result_log(
        qdf, week, ult0, ifwi, results, REPORTS_DIR,
        timings=_timings.get(qdf) if CRONOS_MODE else None,
        content=content,
        vid=vid,
        ult_vid=ult_vid,
    )

    if overall == 'FAIL':
        _status(f'Fused QDF {qdf}: FAILED — see {log_path}', 'fail')
        _alert_popup_async(f'FCO FAILED — {qdf}',
                           f'One or more content items failed.\nSee: {log_path}')
    else:
        _status(f'Fused QDF {qdf}: PASS', 'ok')

    return log_path, overall, result_content


# ---------------------------------------------------------------------------
# SVOS utilities — fused/overwrite helpers
# ---------------------------------------------------------------------------

def _ask_fused() -> bool:
    """Asks whether the unit is fused. Returns True=fused, False=not fused."""
    print()
    print('  Is the unit fused?')
    print('  y - Fused         (continues directly to SVOS boot)')
    print('  n - Not fused     (coordinates with pysv to perform the overwrite first)')
    print()
    while True:
        r = input('  Fused? (y/n): ').strip().lower()
        if r in ('s', 'y'):
            return True
        if r in ('n', 'no'):
            return False
        print('  [!!] Enter y or n.')


def _do_sv_overwrite_wait(qdf: str, ult0: str, soc: str = 'x4', kwargs=None,
                          monitor_label: str | None = None):
    """
    Coordinates with sv_automation (running in pysv) to perform the fuse overwrite.
    1. Writes qdf_list.json with the requested QDF.
    2. Cleans previous signals.
    3. Prints instructions for the user to run sv_automation in pysv.
    4. Waits for sv_automation to write {qdf}_sv_done.signal.
    5. Verifies whether there was an error.
    """
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)

    # Clean old signals
    for name in [f'{qdf}_sv_started.signal', f'{qdf}_sv_done.signal', f'{qdf}_svos_done.signal']:
        sig = SIGNAL_DIR / name
        if sig.exists():
            sig.unlink()
            _status(f'[cleanup] Previous signal removed: {name}', 'info')

    # Write qdf_list.json so sv_automation can read it
    entry = {'qdf': qdf, 'ult0': ult0, 'soc': soc}
    if kwargs:
        entry['kwargs'] = kwargs
    with open(QDF_LIST_FILE, 'w', encoding='utf-8') as f:
        json.dump([entry], f, indent=2)
    _status(f'qdf_list.json written: QDF={qdf}  ULT={ult0}  SOC={soc}', 'info')

    # Instructions for the user
    _print_pysv_overwrite_instructions()
    print(f'  Waiting for overwrite start signal ({qdf}_sv_started.signal)...')
    print(f'  Waiting for overwrite completion signal ({qdf}_sv_done.signal)...')
    print()

    # Wait for either the new start signal or the legacy done signal directly.
    sv_started = SIGNAL_DIR / f'{qdf}_sv_started.signal'
    sv_done = SIGNAL_DIR / f'{qdf}_sv_done.signal'
    first_signal = _wait_for_any_file([sv_started, sv_done], poll=1)

    if first_signal == sv_started:
        _status(f'Overwrite of {qdf} started by sv_automation.', 'info')
        if monitor_label:
            with _monitor_stage(monitor_label):
                _wait_for_file(sv_done, poll=1)
        else:
            _wait_for_file(sv_done, poll=1)
    else:
        _status(f'Overwrite of {qdf} completed without start signal (legacy sv_automation).', 'info')

    content = sv_done.read_text(encoding='utf-8').strip()
    if content == 'error':
        raise FCOStepError(f'sv_automation reported an error during the overwrite of {qdf}. '
                           f'Check the pysv console.')
    _status(f'Overwrite of {qdf} completed by sv_automation.', 'ok')

    # Write svos_done so sv_automation does not remain blocked waiting
    svos_done = SIGNAL_DIR / f'{qdf}_svos_done.signal'
    svos_done.write_text('done\n')
    _status(f'svos_done signal written for {qdf}.', 'info')
    _pause('TEST MODE: Overwrite handshake completed. Press any key to continue...')


def _print_pysv_overwrite_instructions():
    """Prints the canonical pysv commands required for overwrite flow."""
    print()
    print('=' * 60)
    print('  ACTION REQUIRED — Fuse Overwrite')
    print('=' * 60)
    print()
    print('  In your pysv session, run:')
    print()
    print('      import users.mkcummin.fle_bs_wrapper as bs_wrap')
    print('      import sys')
    print(f'      sys.path.insert(0, r\'{BASE_DIR}\')')
    print('      import sv_automation')
    print('      sv_automation.run_qdf_list(itp, sv, bs_wrap)')
    print()


def _show_mode2_centos_pysv_instructions(qdf: str):
    """Shows instructions to run pysv monitor for Mode 2 CentOS power cycle automation."""
    msg_lines = [
        'ACTION REQUIRED — Mode 2 CentOS Power Cycle Automation',
        '',
        'In your pysv session, run:',
        '  import sys',
        f"  sys.path.insert(0, r'{BASE_DIR}')",
        '  import sv_automation',
        '  sv_automation.run_mode2_centos_monitor()',
        '',
        'Leave it running while FCO_AutoTool executes SVOS content.',
        f"It will wait for {qdf}_svos_done.signal and then handle power cycle automatically.",
    ]

    print()
    print('=' * 60)
    for ln in msg_lines:
        print(ln)
    print('=' * 60)
    print()

    popup_msg = (
        'Mode 2 CentOS selected. In pysv run:\n\n'
        'import sys\n'
        f"sys.path.insert(0, r'{BASE_DIR}')\n"
        'import sv_automation\n'
        'sv_automation.run_mode2_centos_monitor()\n\n'
        'Keep it running. It waits for svos_done and performs power cycle automatically.'
    )
    _alert_popup('Mode 2 CentOS — pysv required', popup_msg)





# ---------------------------------------------------------------------------
# SVOS utilities — tools menu
# ---------------------------------------------------------------------------

def _ask_tool_menu() -> str:
    """
    Shows the main tools menu before the FCO flow.
    Returns: 'fco' | 'boot' | 'update' | 'centos_direct' | 'efi_timing'
    Prefix 't' activates TEST_MODE (e.g. t1, t2, t3, t4, t5).
    """
    global TEST_MODE
    print()
    print('=' * 60)
    print('  FCO AUTOMATION — Tools')
    print('=' * 60)
    print()
    print('  1 - FCO Automation          (fuse + test)')
    print('  2 - Boot SVOS only')
    print('  3 - Update SVOS             (osvsetrelease + osvosupdate)')
    print('  4 - Boot CentOS only')
    print('  5 - EFI Timing              (overwrite + time to BIOS/EFI gray screen)')
    print()
    print('  Prefix t for TEST MODE (e.g. t1, t2, t3, t4, t5)')
    print()
    _map = {'1': 'fco', '2': 'boot', '3': 'update', '4': 'centos_direct', '5': 'efi_timing'}
    while True:
        raw = input('Tool (1-5): ').strip().lower()
        if raw.startswith('t') and raw[1:] in _map:
            TEST_MODE = True
            print('  [TEST MODE activated]')
            return _map[raw[1:]]
        if raw in _map:
            return _map[raw]
        print('  [!!] Enter 1, 2, 3, 4 or 5.')


def run_efi_timing(com_port: str):
    """
    Tool 5: Measures timing until BIOS/EFI gray screen is detected.
    - If unit is not fused: measures overwrite duration first.
    - Then measures post-overwrite (or post-start for fused) to BIOS/EFI screen.
    """
    print()
    print('=' * 60)
    print('  EFI TIMING')
    print('=' * 60)
    print()

    ifwi = get_ifwi_version()
    _status(f'IFWI detected: {ifwi}', 'info')
    _set_runtime_monitor_tool('EFI Timing')

    cfg_last = _load_json_file(LAST_EFI_TIMING_CONFIG_FILE)
    use_last = False
    if cfg_last is not None:
        print('  Last EFI Timing configuration found:')
        print(f'    Fused: {cfg_last.get("fused")}')
        print(f'    QDF: {cfg_last.get("qdf", "N/A")}')
        print(f'    ULT: {cfg_last.get("ult0", "N/A")}')
        print(f'    SOC: {cfg_last.get("soc", "x4")}')
        use_last = input('  Use this configuration? (y/n): ').strip().lower() in ('s', 'y', 'yes')

    fused = cfg_last.get('fused') if use_last else _ask_fused()
    qdf = cfg_last.get('qdf', '') if use_last else ''
    ult0 = cfg_last.get('ult0', '') if use_last else ''
    soc = cfg_last.get('soc', 'x4') if use_last else 'x4'
    kwargs = cfg_last.get('kwargs', {}) if use_last else {}
    overwrite_secs = None

    if not fused:
        if not use_last:
            qdfs, ult0, soc, kwargs = _ask_qdf_params()
            if not qdfs:
                print('[!!] ERROR: no QDF entered.')
                return
            if len(qdfs) > 1:
                print('[!] For EFI timing only the first QDF is used for overwrite timing.')
            qdf = qdfs[0]

        try:
            t0_ow = time.time()
            _do_sv_overwrite_wait(qdf, ult0, soc, kwargs,
                                  monitor_label='Bootscript Excecution (Fuse Overwrite)')
            overwrite_secs = time.time() - t0_ow
            _status(f'Overwrite time ({qdf}): {_fmt_dur(overwrite_secs)}', 'ok')
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            _hold_open_on_error('EFI TIMING')
            return

        _save_json_file(LAST_EFI_TIMING_CONFIG_FILE, {
            'fused': False,
            'qdf': qdf,
            'ult0': ult0,
            'soc': soc,
            'kwargs': kwargs,
        })
    else:
        _save_json_file(LAST_EFI_TIMING_CONFIG_FILE, {
            'fused': True,
            'qdf': '',
            'ult0': '',
            'soc': 'x4',
            'kwargs': {},
        })

    _status(f'Opening {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        _pause('Ready to start EFI timing. Press any key...')

        t0_efi = time.time()
        with _monitor_stage('EFI Timing - reboot to BIOS/EFI screen'):
            _status(f'Waiting for system reboot ({BIOS_REBOOT_WAIT}s)...', 'wait')
            time.sleep(BIOS_REBOOT_WAIT)
            s.flush()

            _status('Looking for BIOS/EFI gray screen (Boot Manager menu screen)...', 'wait')
            _status("(Press 's' to skip BIOS wait and mark as timeout)", 'info')
            _wait_for_bios_with_nudge(s, BIOS_WAIT_TIMEOUT, enable_nudge=fused)

        efi_secs = time.time() - t0_efi
        total_secs = (overwrite_secs or 0.0) + efi_secs

        print()
        print('=' * 60)
        print('  EFI TIMING RESULT')
        print('=' * 60)
        if overwrite_secs is not None:
            print(f'  Overwrite ({qdf})                 : {_fmt_hms(overwrite_secs)}')
        else:
            print('  Overwrite                         : N/A (fused mode)')
        print(f'  IFWI used                         : {ifwi}')
        print(f'  Post-overwrite to BIOS/EFI screen : {_fmt_hms(efi_secs)}')
        print(f'  Total measured time               : {_fmt_hms(total_secs)}')
        print('=' * 60)

        # Keep one machine-readable log per run under logs/
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = LOG_DIR / f'EFI_TIMING_{ts}.txt'
        lines = [
            f'Date/Time: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            f'COM: {com_port}',
            f'IFWI: {ifwi}',
            f'Fused: {fused}',
            f'QDF: {qdf or "N/A"}',
            f'Overwrite: {_fmt_hms(overwrite_secs) if overwrite_secs is not None else "N/A"}',
            f'Post_overwrite_to_efi: {_fmt_hms(efi_secs)}',
            f'Total: {_fmt_hms(total_secs)}',
        ]
        out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        _status(f'EFI timing log saved: {out}', 'ok')

    except KeyboardInterrupt:
        _status('Manual stop requested (Ctrl+C). Closing EFI timing tool...', 'info')
    except Exception as e:
        _status(f'EFI timing FAILED: {e}', 'fail')
        logging.error(f'EFI timing failed: {e}', exc_info=True)
        _alert_popup('EFI Timing FAILED', str(e))
        _hold_open_on_error('EFI TIMING')
    finally:
        s.close()
        _status(f'Port {com_port} closed.', 'info')


def _parse_svosinfo(text: str) -> dict:
    """
    Extracts key fields from the svosinfo output.
    Returns a dict with: release, patch, build_date, debian, kernel, bios, cpu_id, cpu_stepping.
    """
    fields = {}
    patterns = {
        'release':      r'SVOS\s+release\s+=\s+(\S+)',
        'patch':        r'SVOS\s+patch\s+=\s+(\S+)',
        'build_date':   r'SVOS\s+build date\s+=\s+(.+)',
        'debian':       r'Debian\s+version\s+=\s+(\S+)',
        'kernel':       r'Kernel\s+version\s+=\s+(\S+)',
        'bios':         r'BIOS\s+version per dmidecode\s+=\s+(.+)',
        'cpu_stepping': r'CPU\s+stepping name\s+=\s+(\S+)',
        'cpu_id':       r'CPU\s+ID\s+=\s+(\S+)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            fields[key] = m.group(1).strip()
    return fields


def run_boot_svos_only(com_port: str):
    """
    Tool 2: Boots SVOS and leaves the session at root@... prompt.
    If the unit is not fused, it coordinates the overwrite with sv_automation (pysv) first.
    """
    print()
    print('=' * 60)
    print('  BOOT SVOS')
    print('=' * 60)

    ifwi = get_ifwi_version()
    _status(f'IFWI detected: {ifwi}', 'info')
    _set_runtime_monitor_tool('Boot SVOS')

    cfg_last = _load_json_file(LAST_BOOT_SVOS_CONFIG_FILE)
    use_last = False
    if cfg_last is not None:
        print('  Last Boot SVOS configuration found:')
        print(f'    Fused: {cfg_last.get("fused")}')
        print(f'    QDF: {cfg_last.get("qdf", "N/A")}')
        print(f'    ULT: {cfg_last.get("ult0", "N/A")}')
        print(f'    SOC: {cfg_last.get("soc", "x4")}')
        use_last = input('  Use this configuration? (y/n): ').strip().lower() in ('s', 'y', 'yes')

    if use_last:
        fused = bool(cfg_last.get('fused', False))
        qdf = cfg_last.get('qdf', '')
        ult0 = cfg_last.get('ult0', '')
        soc = cfg_last.get('soc', 'x4')
        kwargs = cfg_last.get('kwargs', {})
    else:
        fused = _ask_fused()
        qdf = ''
        ult0 = ''
        soc = 'x4'
        kwargs = {}

    if not fused:
        if not use_last:
            qdfs, ult0, soc, kwargs = _ask_qdf_params()
            if not qdfs:
                print('[!!] ERROR: no QDF entered.')
                return
            if len(qdfs) > 1:
                print('[!] For Boot SVOS only the first QDF is used for the overwrite.')
            qdf = qdfs[0]
        try:
            _do_sv_overwrite_wait(qdf, ult0, soc, kwargs,
                                  monitor_label='Bootscript Excecution (Fuse Overwrite)')
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            _hold_open_on_error('BOOT SVOS')
            return

    _save_json_file(LAST_BOOT_SVOS_CONFIG_FILE, {
        'fused': fused,
        'qdf': qdf,
        'ult0': ult0,
        'soc': soc,
        'kwargs': kwargs,
    })

    _status(f'Abriendo {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        _pause('Ready to start SVOS boot. Press a key...')
        boot_svos(s, do_mountsv=False, fused_nudge=fused)
        _status('SVOS ready - root@... prompt active.', 'ok')
        _alert_popup_async('Boot SVOS OK',
                   'SVOS booteo correctamente y quedo activo en prompt root@....')
        print()
        print('=' * 60)
        print('  BOOT COMPLETED')
        print('=' * 60)
        _hold_open_until_interrupt('SVOS')
    except KeyboardInterrupt:
        _status('Manual stop requested (Ctrl+C). Closing SVOS tool...', 'info')
    except Exception as e:
        _status(f'Error during SVOS boot: {e}', 'fail')
        logging.error(f'Boot SVOS failed: {e}', exc_info=True)
        _alert_popup('Boot SVOS FAILED', str(e))
        _hold_open_on_error('BOOT SVOS')
    finally:
        s.close()
        _status(f'Port {com_port} closed.', 'info')


def run_update_svos(com_port: str):
    """
    Tool 3: (overwrite if not fused) -> Boot SVOS -> osvsetrelease
                   -> osvosupdate -> umountsv;mountsv -> svosinfo.
    """
    print()
    print('=' * 60)
    print('  UPDATE SVOS')
    print('=' * 60)

    ifwi = get_ifwi_version()
    _status(f'IFWI detected: {ifwi}', 'info')
    _set_runtime_monitor_tool('Update SVOS')

    cfg_last = _load_json_file(LAST_UPDATE_CONFIG_FILE)
    use_last = False
    if cfg_last is not None:
        print('  Last Update SVOS configuration found:')
        print(f'    Fused: {cfg_last.get("fused")}')
        print(f'    Overwrite QDF: {cfg_last.get("overwrite_qdf", "N/A")}')
        print(f'    ULT: {cfg_last.get("ult0", "N/A")}')
        print(f'    SOC: {cfg_last.get("soc", "x4")}')
        print(f'    Release: {cfg_last.get("release", "")}')
        print(f'    Patch: {cfg_last.get("patch", "")}')
        use_last = input('  Use this configuration? (y/n): ').strip().lower() in ('s', 'y', 'yes')

    if use_last:
        fused = bool(cfg_last.get('fused', False))
        overwrite_qdf = cfg_last.get('overwrite_qdf')
        ult0 = cfg_last.get('ult0', '')
        soc = cfg_last.get('soc', 'x4')
        kwargs = cfg_last.get('kwargs', {})
        release = cfg_last.get('release', '').strip()
        patch = cfg_last.get('patch', '').strip()
    else:
        fused = _ask_fused()
        overwrite_qdf = None
        ult0 = ''
        soc = 'x4'
        kwargs = {}

        if not fused:
            qdfs, ult0, soc, kwargs = _ask_qdf_params()
            if not qdfs:
                print('[!!] ERROR: no QDF entered.')
                return
            if len(qdfs) > 1:
                print('[!] For Update SVOS only the first QDF is used for the overwrite.')
            overwrite_qdf = qdfs[0]

        print()
        release = input('SVOS release (e.g.: dmr2611-bookworm)       : ').strip()
        patch   = input('Patch number (e.g.: 024)                     : ').strip()

    if not release or not patch:
        print('[!!] Release and patch are required.')
        return

    print()
    if overwrite_qdf:
        print(f'  Overwrite  : QDF={overwrite_qdf}  ULT={ult0}  SOC={soc}')
    print(f'  Release    : {release}')
    print(f'  Patch      : {patch}')
    print(f'  Command    : osvsetrelease -r {release} -u -n {patch}')
    print()
    confirm = input('Confirm? (y/n): ').strip().lower()
    if confirm not in ('s', 'y'):
        print('Cancelled.')
        return

    _save_json_file(LAST_UPDATE_CONFIG_FILE, {
        'fused': fused,
        'overwrite_qdf': overwrite_qdf,
        'ult0': ult0,
        'soc': soc,
        'kwargs': kwargs,
        'release': release,
        'patch': patch,
    })

    # --- Preliminary step: overwrite if not fused ---
    if overwrite_qdf:
        try:
            _do_sv_overwrite_wait(overwrite_qdf, ult0, soc, kwargs,
                                  monitor_label='Bootscript Excecution (Fuse Overwrite)')
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            _hold_open_on_error('UPDATE SVOS')
            return

    _status(f'Abriendo {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        total_steps = 5
        step = 1

        # 1. Boot SVOS
        _pause(f'Step {step}/{total_steps} — Boot SVOS. Press a key...')
        _status(f'Step {step}/{total_steps} — Boot SVOS...', 'step')
        boot_svos(s, do_mountsv=False, fused_nudge=fused)
        _status('SVOS ready.', 'ok')
        step += 1

        # 2. osvsetrelease
        _pause(f'Step {step}/{total_steps} — osvsetrelease. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: osvsetrelease -r {release} -u -n {patch}', 'step')
        with _monitor_stage('Update SVOS - osvsetrelease'):
            s.send(f'osvsetrelease -r {release} -u -n {patch}')
            with _guard(f'osvsetrelease -r {release} -u -n {patch}'):
                s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
        _status('osvsetrelease completed.', 'ok')
        step += 1

        # 3. osvosupdate -v
        _pause(f'Step {step}/{total_steps} — osvosupdate. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: osvosupdate -v (may take ~10 min)...', 'step')
        with _monitor_stage('Update SVOS - osvosupdate'):
            s.send('osvosupdate -v')
            with _guard('osvosupdate -v'):
                s.read_until(SVOS_PROMPT, timeout=OSVOSUPDATE_TIMEOUT)
        _status('osvosupdate completed.', 'ok')
        step += 1

        # 4. umountsv; mountsv — with recovery if it hangs
        _pause(f'Step {step}/{total_steps} — umountsv; mountsv. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: umountsv; mountsv...', 'step')
        with _monitor_stage('Update SVOS - umountsv/mountsv'):
            s.send('umountsv; mountsv')
            mountsv_ok = False
            try:
                s.read_until(SVOS_PROMPT, timeout=UPDATE_MOUNTSV_TIMEOUT)
                mountsv_ok = True
                _status('mountsv completed.', 'ok')
            except TimeoutError:
                _status(f'mountsv did not respond within {UPDATE_MOUNTSV_TIMEOUT//60} min — iniciando recovery...', 'warn')
                if fused:
                    # Fused unit: we cannot reboot, we need a manual power cycle
                    _alert_popup('mountsv TIMEOUT — Power Cycle required',
                                 'mountsv no respondio.\n'
                                 'Haz un power cycle manual a la unidad\n'
                                 'y presiona OK cuando el sistema haya arrancado de nuevo.')
                    _status('Waiting for SVOS to return after the power cycle...', 'wait')
                    s.flush()
                    # Wait for the SVOS prompt to appear (without rebooting through BIOS)
                    s.read_until(SVOS_PROMPT, timeout=None)
                    _status('SVOS back after the power cycle.', 'ok')
                else:
                    # Not fused unit: reboot via bootscript
                    _status('Unit not fused — relaunching SVOS boot...', 'step')
                    s.flush()
                    boot_svos(s, do_mountsv=False, fused_nudge=fused)
                    _status('SVOS back after reboot.', 'ok')
                mountsv_ok = True
        step += 1

        # 5. svosinfo + verification
        _pause(f'Step {step}/{total_steps} — svosinfo. Press a key...')
        _status(f'Step {step}/{total_steps} — Verifying with svosinfo...', 'step')
        with _monitor_stage('Update SVOS - svosinfo verification'):
            s.flush()
            s.send('svosinfo')
            with _guard('svosinfo'):
                raw_out = s.read_until(SVOS_PROMPT, timeout=SVOSINFO_TIMEOUT)
        info_text = raw_out.decode('utf-8', errors='replace')
        parsed = _parse_svosinfo(info_text)

        # Show result
        print()
        print('=' * 60)
        print('  SVOSINFO VERIFICATION')
        print('=' * 60)
        rel_ok   = parsed.get('release', '?')
        patch_ok = parsed.get('patch',   '?')
        print(f'  IFWI used         = {ifwi}')
        print(f'  SVOS release      = {rel_ok}')
        print(f'  SVOS patch        = {patch_ok}')
        if parsed.get('build_date'):
            print(f'  SVOS build date   = {parsed["build_date"]}')
        if parsed.get('debian'):
            print(f'  Debian version    = {parsed["debian"]}')
        if parsed.get('kernel'):
            print(f'  Kernel version    = {parsed["kernel"]}')
        if parsed.get('bios'):
            print(f'  BIOS version      = {parsed["bios"]}')
        if parsed.get('cpu_stepping'):
            print(f'  CPU stepping      = {parsed["cpu_stepping"]}')
        print('=' * 60)

        # Validate that it matches the requested values
        if rel_ok == release and patch_ok == patch:
            _status(f'Update SUCCESSFUL: {release} patch {patch}', 'ok')
            _alert_popup_async('Update SVOS OK',
                               f'SVOS updated successfully.\n'
                               f'Release: {release}\nPatch: {patch}')
        else:
            _status(f'WARNING: release/patch do not match. '
                    f'Expected: {release}/{patch}  '
                    f'Detected: {rel_ok}/{patch_ok}', 'warn')
            _alert_popup('Update SVOS — Verify',
                         f'Release or patch do not match.\n'
                         f'Expected: {release} / {patch}\n'
                         f'Detected: {rel_ok} / {patch_ok}')

    except Exception as e:
        _status(f'Error during SVOS update: {e}', 'fail')
        logging.error(f'Update SVOS failed: {e}', exc_info=True)
        _alert_popup('Update SVOS FAILED', str(e))
        _hold_open_on_error('UPDATE SVOS')
    finally:
        s.close()
        _status(f'Port {com_port} closed.', 'info')


def run_boot_centos_direct(com_port: str):
    """
    Tool 4: Boots CentOS and validates shell readiness.
    If the unit is not fused, it coordinates overwrite with sv_automation (pysv) first.
    If the unit is fused, it continues directly to CentOS boot.
    """
    print()
    print('=' * 60)
    print('  BOOT CENTOS')
    print('=' * 60)
    print()

    ifwi = get_ifwi_version()
    _status(f'IFWI detected: {ifwi}', 'info')
    _set_runtime_monitor_tool('Boot CentOS')

    cfg_last = _load_json_file(LAST_BOOT_CENTOS_CONFIG_FILE)
    use_last = False
    if cfg_last is not None:
        print('  Last Boot CentOS configuration found:')
        print(f'    Fused: {cfg_last.get("fused")}')
        print(f'    QDF: {cfg_last.get("qdf", "N/A")}')
        print(f'    ULT: {cfg_last.get("ult0", "N/A")}')
        print(f'    SOC: {cfg_last.get("soc", "x4")}')
        use_last = input('  Use this configuration? (y/n): ').strip().lower() in ('s', 'y', 'yes')

    if use_last:
        fused = bool(cfg_last.get('fused', False))
        qdf = cfg_last.get('qdf', '')
        ult0 = cfg_last.get('ult0', '')
        soc = cfg_last.get('soc', 'x4')
        kwargs = cfg_last.get('kwargs', {})
    else:
        fused = _ask_fused()
        qdf = ''
        ult0 = ''
        soc = 'x4'
        kwargs = {}

    did_wrapper = False

    if not fused:
        if not use_last:
            qdfs, ult0, soc, kwargs = _ask_qdf_params()
            if not qdfs:
                print('[!!] ERROR: no QDF entered.')
                return
            if len(qdfs) > 1:
                print('[!] For Boot CentOS only the first QDF is used for wrapper overwrite.')
            qdf = qdfs[0]
        try:
            _do_sv_overwrite_wait(qdf, ult0, soc, kwargs,
                                  monitor_label='Bootscript Excecution (Fuse Overwrite)')
            did_wrapper = True
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            _hold_open_on_error('BOOT CENTOS')
            return

    _save_json_file(LAST_BOOT_CENTOS_CONFIG_FILE, {
        'fused': fused,
        'qdf': qdf,
        'ult0': ult0,
        'soc': soc,
        'kwargs': kwargs,
    })

    _status(f'Opening {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        _pause('Ready to start CentOS boot. Press a key...')

        # If wrapper was not executed, try reboot from current shell first.
        if not did_wrapper:
            _status('Sending reboot command before BIOS navigation...', 'info')
            try:
                s.send('reboot')
                time.sleep(1)
            except Exception as e:
                _status(f'Could not send reboot: {e}. Continuing with BIOS wait...', 'warn')

        with _monitor_stage('Boot CentOS - BIOS/EFI and login validation'):
            boot_centos(s, fused_nudge=fused)
        _status('CentOS Boot: PASS', 'ok')
        _alert_popup_async('CentOS Boot OK',
                           'CentOS booteo correctamente (login + ifconfig).')

        print()
        print('=' * 60)
        print('  BOOT COMPLETED')
        print('=' * 60)
        _hold_open_until_interrupt('CENTOS')
    except KeyboardInterrupt:
        _status('Manual stop requested (Ctrl+C). Closing CentOS tool...', 'info')
    except Exception as e:
        _status(f'Error during CentOS boot: {e}', 'fail')
        logging.error(f'Boot CentOS failed: {e}', exc_info=True)
        _alert_popup('CentOS Boot FAILED', str(e))
        _hold_open_on_error('BOOT CENTOS')
    finally:
        s.close()
        _status(f'Port {com_port} closed.', 'info')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='FCO SVOS Serial Automation')
    parser.add_argument('--config', default=str(QDF_LIST_FILE),
                        help='Path to the JSON file with the QDF list')
    args = parser.parse_args()

    # ---- Setup ----
    _update_readme()
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_DIR / f'FCO_AutoTool_{ts}.log')
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    _apply_timeouts_from_file()
    _ask_runtime_profile()

    logging.info('=== FCO SVOS Automation ===')

    global TEST_MODE

    # ---- Main tools menu ----
    tool = _ask_tool_menu()

    if tool in ('boot', 'update', 'centos_direct', 'efi_timing'):
        # Tools that only need COM port (no week or FCO mode)
        print()
        com_port = input('COM port (e.g.: COM9 or just 9): ').strip()
        if not com_port.upper().startswith('COM'):
            com_port = 'COM' + com_port

        if tool == 'boot':
            run_boot_svos_only(com_port)
        elif tool == 'update':
            run_update_svos(com_port)
        elif tool == 'centos_direct':
            run_boot_centos_direct(com_port)
        else:  # tool == 'efi_timing'
            run_efi_timing(com_port)
        if tool == 'update':
            _pause_before_close('UPDATE SVOS')
        elif tool == 'efi_timing':
            _pause_before_close('EFI TIMING')
        return

    # ---- From here: tool == 'fco' ----

    # ---- Last saved configuration? ----
    _cfg_last = _load_last_config()
    use_last  = False
    if _cfg_last is not None:
        _print_last_config(_cfg_last)
        use_last = input('  Use this configuration? (y/n): ').strip().lower() in ('s', 'y', 'yes')
        if use_last:
            TEST_MODE = _cfg_last.get('test_mode', False)

    # ---- Execution mode ----
    if use_last:
        mode = _cfg_last['mode']
        print(f'\n  Mode: {mode} — {_MODE_DESC.get(mode, "")}')
    else:
        mode = _ask_mode()

    _set_runtime_monitor_tool(f'FCO Automation - Mode {mode}')

    # ---- COM port and week ----
    if use_last:
        com_port = _cfg_last['com_port']
        week     = _cfg_last['week']
        print(f'  COM: {com_port}  |  Week: WW{week}')
    else:
        com_port, week = _ask_com_week()

    # Read IFWI only once (it is the same for all QDFs)
    ifwi = get_ifwi_version()
    logging.info(f'IFWI detected: {ifwi}')

    all_results   = []
    summary_ult0    = 'N/A'
    summary_vid     = ''
    summary_ult_vid = ''
    s             = None

    try:
        if mode in (1, 4):
            if use_last:
                qdf_list = _cfg_last['qdf_list']
                ult0     = qdf_list[0]['ult0']
                soc      = qdf_list[0].get('soc', 'x4')
                print()
                print('  QDFs to process:')
                for item in qdf_list:
                    extra_str   = f"  kwargs={item['kwargs']}" if item.get('kwargs') else ''
                    content_str = 'full' if item.get('content') is None else ', '.join(item['content'])
                    print(f'    {item["qdf"]:<12}  ULT={item["ult0"]}  SOC={item["soc"]}  '
                          f'content={content_str}{extra_str}')
                print()
            else:
                qdfs, ult0, soc, kwargs = _ask_qdf_params()
                if not qdfs:
                    print('\n[!!] ERROR: no QDF entered.')
                    input('\nPress ENTER to close...')
                    sys.exit(1)

                qdf_list = [{'qdf': q, 'ult0': ult0, 'soc': soc, 'kwargs': kwargs}
                            for q in qdfs]

                print()
                print('  QDFs to process:')
                for item in qdf_list:
                    extra_str = f"  kwargs={item['kwargs']}" if item['kwargs'] else ''
                    print(f'    {item["qdf"]:<12}  ULT={item["ult0"]}  SOC={item["soc"]}{extra_str}')
                print()
                _ask_content_config(qdf_list)
                _save_last_config({
                    'mode': mode, 'test_mode': TEST_MODE,
                    'com_port': com_port, 'week': week,
                    'qdf_list': qdf_list,
                })

            with open(QDF_LIST_FILE, 'w') as f:
                json.dump(qdf_list, f, indent=2)

            logging.info(f'COM: {com_port} | Mode: {mode} | Week: WW{week} | '
                         f'ULT: {ult0} | SOC: {soc} | QDFs: {[i["qdf"] for i in qdf_list]}')

            _clean_signals(qdf_list)
            logging.info('Signals anteriores eliminadas.')

            s = _open_serial(com_port)
            s.flush()

            if mode == 4:
                print()
                _status('Mode 4: Fused unit — ignoring the current boot.', 'info')
                _status('        Waiting for SV to start the overwrite...', 'info')

            skip_boot_fco = _decide_skip_boot_from_serial(s)

            summary_ult0 = ult0
            all_results  = _run_main_loop(s, qdf_list, week, ult0, ifwi, mode,
                                          skip_initial_boot=skip_boot_fco)

        elif mode == 2:
            if use_last:
                qdf           = _cfg_last['fused_qdf']
                ult0          = _cfg_last['fused_ult0']
                fused_ult_vid = _cfg_last.get('fused_ult_vid', _cfg_last.get('fused_ult0', ''))
                fused_vid     = _cfg_last.get('fused_vid', '')
                soc           = _cfg_last.get('fused_soc', 'x4')
                fused_content = _cfg_last.get('fused_content')
                print(f'\n  Fused QDF: {qdf}  ULT/VID={fused_ult_vid or "N/A"}  SOC={soc}')
                content_str = 'full' if fused_content is None else ', '.join(fused_content)
                print(f'  Content: {content_str}')
            else:
                print('\nEnter fused unit data for Mode 2:')
                qdf = _ask_single_qdf('(fused)')
                fused_ult_vid = input('ULT o VID (logs/summary only)        : ').strip()
                if fused_ult_vid.upper() == 'N/A':
                    fused_ult_vid = 'N/A'
                ult0 = 'N/A'
                fused_vid = ''
                soc = 'x4'
                kwargs = {}
                _fused_list = [{'qdf': qdf}]
                _ask_content_config(_fused_list)
                fused_content = _fused_list[0].get('content')
                _save_last_config({
                    'mode': mode, 'test_mode': TEST_MODE,
                    'com_port': com_port, 'week': week,
                    'fused_qdf': qdf, 'fused_ult0': ult0, 'fused_soc': soc,
                    'fused_ult_vid': fused_ult_vid,
                    'fused_vid': fused_vid,
                    'fused_kwargs': kwargs, 'fused_content': fused_content,
                })

            logging.info(f'COM: {com_port} | Mode: 2 | Week: WW{week} | '
                         f'ULT: {ult0} | Fused QDF: {qdf}')

            # Persist fused mode-2 context so pysv monitor can auto-detect QDF.
            with open(QDF_LIST_FILE, 'w', encoding='utf-8') as f:
                json.dump([{'qdf': qdf, 'ult0': ult0, 'soc': soc, 'content': fused_content}], f, indent=2)

            s = _open_serial(com_port)
            s.flush()
            skip_boot_fused = _decide_skip_boot_from_serial(s)

            print(f'\n{"="*60}')
            _status(f'Mode 2 — Testing fused QDF: {qdf}', 'step')
            print(f'{"="*60}')
            print()
            _needs_svos = fused_content is None or any(
                t in fused_content for t in CONTENT_TESTS if t != 'centos_boot'
            )
            print('  [INFO] Mode 2: fused unit only (no pysv overwrite)')
            if _should_run(fused_content, 'centos_boot') and _needs_svos:
                print('  CentOS boot selected with SVOS tests: run the pysv monitor once:')
                print('    - It waits for svos_done automatically')
                print('    - Then it performs the power cycle automatically')
            elif _should_run(fused_content, 'centos_boot'):
                print('  CentOS-only: no pysv needed, navigating BIOS directly.')
            print()

            if _should_run(fused_content, 'centos_boot') and _needs_svos:
                _show_mode2_centos_pysv_instructions(qdf)

            log_path, overall, result_content = run_fused_test(s, qdf, ult0, week, ifwi,
                                                               content=fused_content, mode=mode,
                                                               vid=fused_vid,
                                                               ult_vid=fused_ult_vid,
                                                               skip_boot=skip_boot_fused)
            summary_ult0 = ult0
            summary_vid = fused_vid
            summary_ult_vid = fused_ult_vid
            all_results.append({'qdf': qdf, 'overall': overall, 'log': str(log_path),
                                 'result_content': result_content})

        elif mode == 3:
            if use_last:
                qdf_fused        = _cfg_last['fused_qdf']
                ult0_f           = _cfg_last['fused_ult0']
                soc_f            = _cfg_last.get('fused_soc', 'x4')
                fused_content_m3 = _cfg_last.get('fused_content')
                qdf_list         = _cfg_last['overwrite_qdf_list']
                ult0_ow          = qdf_list[0]['ult0']
                soc_ow           = qdf_list[0].get('soc', 'x4')
                print(f'\n  Fused QDF: {qdf_fused}  ULT={ult0_f}  SOC={soc_f}')
                fc_str = 'full' if fused_content_m3 is None else ', '.join(fused_content_m3)
                print(f'  Fused content: {fc_str}')
                print(f'  QDFs overwrite: {", ".join(i["qdf"] for i in qdf_list)}')
                for item in qdf_list:
                    c_str = 'full' if item.get('content') is None else ', '.join(item['content'])
                    print(f'    {item["qdf"]:<12}  ULT={item["ult0"]}  content={c_str}')
            else:
                # PHASE A: fused QDF
                print('\n[Phase A] Enter fused QDF to test first:')
                qdf_fused = _ask_single_qdf('(fused)')

                # Content configuration for the fused QDF
                print('\n[Phase A Content] Configure the tests for the fused QDF:')
                _fused_list_m3 = [{'qdf': qdf_fused}]
                _ask_content_config(_fused_list_m3)
                fused_content_m3 = _fused_list_m3[0].get('content')

                # PHASE B: QDFs to overwrite
                print('\n[Phase B] Enter QDFs to overwrite after the fused test:')
                print('          (ULT here is shared with the fused unit)')
                qdfs_ow, ult0_ow, soc_ow, kwargs_ow = _ask_qdf_params('(QDFs to overwrite)')
                if not qdfs_ow:
                    print('\n[!!] ERROR: no QDFs to overwrite entered.')
                    input('\nPress ENTER to close...')
                    sys.exit(1)

                # Reuse overwrite ULT/SOC for fused-phase metadata/log context.
                ult0_f = ult0_ow
                soc_f = soc_ow

                qdf_list = [{'qdf': q, 'ult0': ult0_ow, 'soc': soc_ow, 'kwargs': kwargs_ow}
                            for q in qdfs_ow]

                # Content configuration for the QDFs to overwrite
                print('\n[Phase B Content] Configure the tests for the QDFs to overwrite:')
                _ask_content_config(qdf_list)

                _save_last_config({
                    'mode': mode, 'test_mode': TEST_MODE,
                    'com_port': com_port, 'week': week,
                    'fused_qdf': qdf_fused, 'fused_ult0': ult0_f, 'fused_soc': soc_f,
                    'fused_content': fused_content_m3,
                    'overwrite_qdf_list': qdf_list,
                })

            logging.info(f'COM: {com_port} | Mode: 3 | Week: WW{week} | '
                         f'Fused QDF: {qdf_fused} | QDFs overwrite: {[i["qdf"] for i in qdf_list]}')

            s = _open_serial(com_port)
            s.flush()
            skip_boot_phase_a = _decide_skip_boot_from_serial(s)

            # PHASE A: test the fused QDF
            print(f'\n{"="*60}')
            _status(f'PHASE A — Testing fused QDF: {qdf_fused}', 'step')
            print(f'{"="*60}')

            log_path_f, overall_f, result_content_f = run_fused_test(s, qdf_fused, ult0_f, week, ifwi,
                                                                      content=fused_content_m3, mode=mode,
                                                                      soc=soc_f, kwargs=None,
                                                                      skip_boot=skip_boot_phase_a)
            all_results.append({'qdf': qdf_fused, 'overall': overall_f, 'log': str(log_path_f),
                                 'result_content': result_content_f})

            # Transition to PHASE B
            print(f'\n{"="*60}')
            print('  PHASE A completed. Start sv_automation now.')
            print('  When sv_automation is ready, press ENTER to continue...')
            print(f'{"="*60}')
            input()

            # Save qdf_list.json and clean signals for PHASE B
            with open(QDF_LIST_FILE, 'w') as f:
                json.dump(qdf_list, f, indent=2)
            _clean_signals(qdf_list)
            logging.info('Previous signals removed (start of PHASE B).')

            print(f'\n{"="*60}')
            _status(f'PHASE B — Overwrite + test of {len(qdf_list)} QDF(s)', 'step')
            print(f'{"="*60}')

            summary_ult0 = ult0_ow
            loop_results = _run_main_loop(s, qdf_list, week, ult0_ow, ifwi, mode)
            all_results.extend(loop_results)

    except KeyboardInterrupt:
        logging.warning('Interrupted by the user.')
    except Exception as e:
        logging.error(f'Error inesperado: {e}', exc_info=True)
        print(f'\n[!!] UNEXPECTED ERROR: {e}')
    finally:
        if s is not None:
            try:
                s.close()
                logging.info('Serial port closed.')
            except Exception:
                pass

    # ---- Summary file for all QDFs ----
    if all_results:
        try:
            summary_path = write_summary_log(week, summary_ult0, ifwi, all_results, REPORTS_DIR,
                                             vid=summary_vid, ult_vid=summary_ult_vid)
            _status(f'Summary saved: {summary_path.name}', 'ok')
        except Exception as e:
            _status(f'Could not write the summary: {e}', 'fail')

    # ---- Final summary ----
    print('\n' + '=' * 60)
    print('  FINAL SUMMARY')
    print('=' * 60)
    for r in all_results:
        ov = r['overall']
        if ov == 'PASS':
            tag = '[PASS]       '
        elif ov == 'RETRY_PASS':
            tag = '[RETRY PASS] '
        elif ov in ('RETRY_FAIL', 'RETRY_ERROR'):
            tag = '[RETRY FAIL] '
        else:
            tag = '[FAIL]       '
        print(f"  {r['qdf']:<12} {ov:<20} {tag}")
    print('=' * 60)
    _pause_before_close('FCO AUTOMATION')


if __name__ == '__main__':
    _self_update()
    try:
        main()
    except Exception as e:
        print(f'\n[!!] ERROR at startup: {e}')
        import traceback
        traceback.print_exc()
        _pause_before_close('FCO AUTOMATION STARTUP')

