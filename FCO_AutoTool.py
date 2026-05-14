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
from pathlib import Path


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
    """Blocking Windows popup + prints to console."""
    print(f'\n[!!] {title}\n     {msg}\n', flush=True)
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:
        pass


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
QDF_LIST_FILE    = BASE_DIR / 'qdf_list.json'
LAST_CONFIG_FILE = BASE_DIR / 'last_config.json'

# GitHub repo for auto-update
GITHUB_REPO_URL = 'https://github.com/egmonter/FCO_AutoTool.git'

BAUDRATE      = 115200

# BIOS screen identifier text — any of these is accepted
BIOS_BANNERS   = [b'OAKSTREAM', b'EDKII', b'Intel Corporation',
                  b'Boot Manager', b'UEFI', b'EDK II']
BIOS_BOOT_MGR  = 'Boot Manager'        # option in the BIOS menu
BIOS_INT_SHELL = 'Internal Shell'      # option inside Boot Manager
BIOS_NAV_MAX   = 20                    # maximum down-arrow presses before error

# EFI Shell prompts (adjust if they differ on your platform)
EFI_PROMPTS   = [b'Shell>', b'shell>', b'EFI Shell']
SVOS_PROMPT   = b'root@sut:'  # SVOS shell prompt (without /> due to interleaved color codes)
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

# Rockets that run in the normal flow (cpu and iax)
ROCKET_CMDS = [
    ('rocket --cfgs --atlas "--hw dram,cpu" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_cpu.txt', 'rocket_dram_cpu'),
    ('rocket --cfgs --atlas "--hw dram,iax" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_iax.txt', 'rocket_dram_iax'),
]

# Rocket dsa: runs at the end with the special killmax/unmountsv/rmmodsvos2/mountsv sequence
ROCKET_DSA_CMD = ('rocket --cfgs --atlas "--hw dram,dsa" -M 5; rtm -c rtm.cfg -M 5 -f rocket_dram_dsa.txt', 'rocket_dram_dsa')

SOLAR_CMD = ('/usr/bin/solar/solar.sh /meshgv '
             '-ratioPUnit0 "" -ratioPUnit1 "" -ratioPUnit2 P0...Pn '
             '-ratioPUnit3 "" -ratioPUnit4 "" -ratioPUnit5 P0...Pn '
             '-ratioPUnit2f1 P0...Pn -ratioPUnit5f1 P0...Pn /log .')

# Canonical content keys (display order for the user)
CONTENT_TESTS = ('supercollider', 'rocket', 'memicals', 'mlc', 'solar', 'centos_boot')

# Display names per content key
_CONTENT_DISPLAY = {
    'supercollider': 'SuperCollider',
    'rocket':        'Rocket (cpu/iax/dsa)',
    'memicals':      'Memicals',
    'mlc':           'MLC',
    'solar':         'Solar',
    'centos_boot':   'CentOS Boot (root/root + ifconfig)',
}

# Display commands for the result log (without file redirection)
CONTENT_CMDS = {
    'supercollider':   'sc -M 5',
    'rocket_dram_cpu': 'rocket --cfgs --atlas "--hw dram,cpu" -M 5; rtm -c rtm.cfg -M 5',
    'rocket_dram_iax': 'rocket --cfgs --atlas "--hw dram,iax" -M 5; rtm -c rtm.cfg -M 5',
    'rocket_dram_dsa': 'rocket --cfgs --atlas "--hw dram,dsa" -M 5; rtm -c rtm.cfg -M 5',
    'memicals':        'memic.py -M 15 memicals:high-mem:proc -X proc:0,1,2,3',
    'mlc':             'mlc --loaded_latency -t60 -Mdatapattern_halfA_half5.txt',
    'solar':           ('/usr/bin/solar/solar.sh /meshgv -ratioPUnit0 "" -ratioPUnit1 "" '
                        '-ratioPUnit2 P0...Pn -ratioPUnit3 "" -ratioPUnit4 "" '
                        '-ratioPUnit5 P0...Pn -ratioPUnit2f1 P0...Pn -ratioPUnit5f1 P0...Pn /log .'),
    'centos_boot':     '\\efi\\boot\\BootCentosDMR.efi (login: root/root, ifconfig check)',
}


# ---------------------------------------------------------------------------
# File signal helpers (coordination with sv_automation)
# ---------------------------------------------------------------------------

def _wait_for_file(filepath, poll: float = 10):
    """Waits indefinitely until the file exists."""
    while not Path(filepath).exists():
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
# Serial helper
# ---------------------------------------------------------------------------

class SVOSSession:
    """Wraps pyserial with read_until / send helpers."""

    def __init__(self, port, baudrate=BAUDRATE):
        self.port    = port
        self.ser     = serial.Serial(port, baudrate, timeout=0.1)
        self.log     = logging.getLogger('serial')
        self.buf     = b''

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
        self.ser.write((cmd + '\r\n').encode())

    def send_slow(self, cmd: str, char_delay: float = 0.05):
        """
        Sends the command character by character with a delay between each one.
        Use when the terminal cannot process fast input (e.g.: EFI Shell).
        """
        self.log.info(f'>>> (slow) {cmd!r}')
        for ch in cmd:
            self.ser.write(ch.encode())
            time.sleep(char_delay)
        time.sleep(0.1)  # pause before Enter
        self.ser.write(b'\r\n')

    def send_enter(self):
        self.ser.write(b'\r\n')

    def send_escape(self):
        """Sends the ESC key to go back to the previous menu in BIOS."""
        self.ser.write(b'\x1b')

    def send_key(self, raw: bytes):
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

    @staticmethod
    def _print(data: bytes):
        try:
            print(data.decode('utf-8', errors='replace'), end='', flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# BIOS navigation
# ---------------------------------------------------------------------------

def navigate_bios_menu(s: SVOSSession, target: str, max_steps: int = BIOS_NAV_MAX,
                       arrow_delay: float = 1.0):
    """
    Navigates the BIOS menu with down arrows looking for `target` as the HIGHLIGHTED item.
    EDK2 highlights the active item with ANSI color ESC[37mESC[40m (white on black).
    When found: waits 1s and presses Enter.
    Raises FCOStepError if not found within max_steps attempts.
    """
    for attempt in range(max_steps):
        raw, _ = s.read_screen(wait=0.5)
        highlighted = _highlighted_items(raw)
        _status(f'Attempt {attempt+1}/{max_steps} - highlighted: {highlighted}', 'wait')

        if any(target in h for h in highlighted):
            _status(f'"{target}" is highlighted -> Enter in 1s', 'ok')
            time.sleep(1.0)
            s.send_enter()
            time.sleep(0.5)
            return

        s.send_arrow_down()
        time.sleep(arrow_delay)

    raise FCOStepError(f'Could not find "{target}" highlighted in BIOS menu after {max_steps} attempts.')


BIOS_NAV_RETRIES = 5  # maximum retries with ESC before giving up


def _wait_for_bios_with_nudge(s: SVOSSession, timeout: int):
    """
    Actively waits for the BIOS screen. If no data arrives (unit already in BIOS
    with a static screen), sends keys every BIOS_NUDGE_INTERVAL seconds to
    force the firmware to redraw and send its content over serial.

    Nudge sequence: ESC -> down arrow -> up arrow (cycling).
    ESC is harmless in the main menu; arrows only move the selection,
    which is corrected when entering the later navigate_bios_menu.
    """
    nudge_keys = [b'\x1b', b'\x1b[B', b'\x1b[A']  # ESC, down, up
    nudge_idx  = 0
    deadline   = time.time() + timeout

    while True:
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

        if time.time() >= deadline:
            raise BiosTimeoutError(
                f'BIOS screen did not appear within {timeout}s ({timeout//60} min)')

        key = nudge_keys[nudge_idx % len(nudge_keys)]
        nudge_idx += 1
        _status('Static BIOS screen — sending key to refresh...', 'wait')
        s.send_key(key)
        time.sleep(0.3)


def boot_svos(s: SVOSSession, do_mountsv: bool = True):
    """
    Secuencia completa de arranque:
      BIOS (OAKSTREAM) -> Boot Manager Menu -> UEFI Internal Shell
      -> FS0: -> \\efi\\boot\\BootSvosDMR.efi -> ENTER (ATTENTION) -> login
      -> mountsv (solo si do_mountsv=True)
    """
    # 1. Wait for the BIOS screen (the system needs time to reboot)
    _status(f'Waiting for system reboot ({BIOS_REBOOT_WAIT}s)...', 'wait')
    time.sleep(BIOS_REBOOT_WAIT)
    s.flush()  # flush accumulated buffer during reboot

    _status('Looking for BIOS screen...', 'wait')
    _status('(If the unit is already in BIOS, keys will be sent automatically to refresh)', 'info')
    _wait_for_bios_with_nudge(s, BIOS_WAIT_TIMEOUT)
    _status('BIOS detected.', 'ok')
    time.sleep(1)

    # 2+3. Navigate Boot Manager Menu -> UEFI Internal Shell with retry via ESC
    for nav_retry in range(BIOS_NAV_RETRIES):
        try:
            _status(f'Looking for Boot Manager Menu (attempt {nav_retry+1}/{BIOS_NAV_RETRIES})...', 'wait')
            navigate_bios_menu(s, BIOS_BOOT_MGR, arrow_delay=1.0)

            # Verify that we entered Boot Manager Menu and not another menu (e.g. Boot Maintenance Manager)
            time.sleep(1.0)
            raw, screen_text = s.read_screen(wait=1.0)
            if 'Maintenance' in screen_text and 'Boot Manager Menu' not in screen_text:
                _status('Entered Boot Maintenance Manager by mistake. Sending ESC...', 'warn')
                s.send_escape()
                time.sleep(1.5)
                continue  # retry from the top of the loop
            _status('Inside Boot Manager Menu.', 'ok')

            _status(f'Looking for UEFI Internal Shell...', 'step')
            navigate_bios_menu(s, BIOS_INT_SHELL, arrow_delay=1.0)
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

    # 4. Wait for EFI Shell prompt
    _status('Waiting for EFI Shell...', 'wait')
    with _guard('EFI Shell prompt'):
        s.read_until_any(EFI_PROMPTS, timeout=BOOT_TIMEOUT)
    _status('EFI Shell ready.', 'ok')

    # 5. Look for BootSVOS.efi on FS0, FS1, FS2
    booted = False
    for fs in ('FS0', 'FS1', 'FS2'):
        _status(f'Trying {fs}: ...', 'step')
        s.flush()
        s.send(f'{fs}:')
        try:
            s.read_until(f'{fs}:\\', timeout=15)
        except TimeoutError:
            _status(f'{fs}: not available, trying next...', 'info')
            continue

        _status(f'Lanzando \\efi\\boot\\BootSvosDMR.efi desde {fs}: ...', 'step')
        s.send_slow('\\efi\\boot\\BootSvosDMR.efi', char_delay=0.05)

        # If the file does NOT exist: the EFI shell returns the prompt in < 5s
        # If the file DOES exist: there is no response for several seconds while it loads
        # -> Wait 10s for the shell prompt; if it does not return = it is loading
        ATTENTION  = b'Press <ENTER> within 10 seconds to drop to a login shell'
        EFI_PROMPT = [b'Shell>', b'shell>', f'{fs}:\\'.encode(), f'{fs}:/'.encode()]
        try:
            matched, _ = s.read_until_any([ATTENTION] + EFI_PROMPT, timeout=10)
            if matched == ATTENTION:
                booted = True
                break
            else:
                # The shell returned the prompt = file not found
                _status(f'BootSvosDMR.efi not found on {fs}: (prompt returned quickly), trying next...', 'info')
                continue
        except TimeoutError:
            # No prompt within 10s = the file loaded and is booting, wait without limit
            _status(f'BootSvosDMR.efi loading on {fs}:, waiting for ATTENTION without limit...', 'wait')
            s.read_until(ATTENTION.decode(), timeout=None)
            booted = True
            break

    if not booted:
        raise FCOStepError(
            'No se encontro \\efi\\boot\\BootSvosDMR.efi en FS0:, FS1: ni FS2:. '
            'Verifica que el filesystem este disponible.')

    _status('ATTENTION message detected. Sending ENTER...', 'step')
    s.send_enter()

    # 7. Wait for the first root@sut:/> (temporary post-ATTENTION shell)
    _status('Loading SVOS...', 'wait')
    with _guard('shell SVOS post-boot (root@sut:/>)'):
        s.read_until(SVOS_PROMPT, timeout=SVOS_TIMEOUT)
    _status('Temporary shell ready. Running login...', 'step')
    s.send('login')

    # 8. Login: esperar "sut login:", enviar usuario y clave
    with _guard('prompt "sut login:" - verify that SVOS loaded correctly'):
        s.read_until('sut login:', timeout=30)
    _status('Entering user: root', 'step')
    s.send('root')

    with _guard('prompt "Password:"'):
        s.read_until_any(['Password:', 'password:'], timeout=30)
    _status('Entering password...', 'step')
    s.send('svos')

    # 9. Wait for the root@sut:/> prompt (authenticated session)
    with _guard('successful login - verify user/password (root/svos)'):
        s.read_until(SVOS_PROMPT, timeout=30)
    _status('Login successful. SVOS shell ready (root@sut:/>).', 'ok')

    if not do_mountsv:
        return

    _pause('Login OK — validate in Raritan and press any key to run mountsv...')

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
    Assumes BootCentosDMR.efi is already executing (BIOS navigation skipped).
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


def boot_centos(s: SVOSSession):
    """
    CentOS boot sequence:
      BIOS -> Boot Manager Menu -> UEFI Internal Shell
      -> FS0/FS1/FS2 -> \\efi\\boot\\BootCentosDMR.efi -> login root/root
      -> ifconfig (basic sanity check)
    """
    _status(f'Waiting for system reboot ({BIOS_REBOOT_WAIT}s)...', 'wait')
    time.sleep(BIOS_REBOOT_WAIT)
    s.flush()

    _status('Looking for BIOS screen...', 'wait')
    _status('(If the unit is already in BIOS, keys will be sent automatically to refresh)', 'info')
    _wait_for_bios_with_nudge(s, BIOS_WAIT_TIMEOUT)
    _status('BIOS detected.', 'ok')
    time.sleep(1)

    for nav_retry in range(BIOS_NAV_RETRIES):
        try:
            _status(f'Looking for Boot Manager Menu (attempt {nav_retry+1}/{BIOS_NAV_RETRIES})...', 'wait')
            navigate_bios_menu(s, BIOS_BOOT_MGR, arrow_delay=1.0)

            time.sleep(1.0)
            _, screen_text = s.read_screen(wait=1.0)
            if 'Maintenance' in screen_text and 'Boot Manager Menu' not in screen_text:
                _status('Entered Boot Maintenance Manager by mistake. Sending ESC...', 'warn')
                s.send_escape()
                time.sleep(1.5)
                continue
            _status('Inside Boot Manager Menu.', 'ok')

            _status('Looking for UEFI Internal Shell...', 'step')
            navigate_bios_menu(s, BIOS_INT_SHELL, arrow_delay=1.0)
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

    _status('Waiting for EFI Shell...', 'wait')
    with _guard('EFI Shell prompt'):
        s.read_until_any(EFI_PROMPTS, timeout=BOOT_TIMEOUT)
    _status('EFI Shell ready.', 'ok')

    booted = False
    login_seen = False
    for fs in ('FS0', 'FS1', 'FS2'):
        _status(f'Trying {fs}: ...', 'step')
        s.flush()
        s.send(f'{fs}:')
        try:
            s.read_until(f'{fs}:\\', timeout=15)
        except TimeoutError:
            _status(f'{fs}: not available, trying next...', 'info')
            continue

        _status(f'Launching \\efi\\boot\\BootCentosDMR.efi from {fs}: ...', 'step')
        s.send_slow('\\efi\\boot\\BootCentosDMR.efi', char_delay=0.05)

        EFI_PROMPT = [b'Shell>', b'shell>', f'{fs}:\\'.encode(), f'{fs}:/'.encode()]
        try:
            matched, _ = s.read_until_any(CENTOS_LOGIN_PROMPTS + EFI_PROMPT, timeout=10)
            if matched in CENTOS_LOGIN_PROMPTS:
                booted = True
                login_seen = True
                break
            _status(f'BootCentosDMR.efi not found on {fs}: (prompt returned quickly), trying next...', 'info')
            continue
        except TimeoutError:
            _status(f'BootCentosDMR.efi loading on {fs}:, waiting for login prompt...', 'wait')
            s.read_until_any(CENTOS_LOGIN_PROMPTS, timeout=CENTOS_BOOT_TIMEOUT)
            booted = True
            login_seen = True
            break

    if not booted:
        raise FCOStepError(
            'Could not find \\efi\\boot\\BootCentosDMR.efi on FS0:, FS1: or FS2:. '
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


# ---------------------------------------------------------------------------
# Workflow per QDF
# ---------------------------------------------------------------------------


def setup_fco_dir(s: SVOSSession, qdf: str, week: str) -> str:
    """Creates and enters the working directory for the QDF."""
    work_dir = f'/root/FCO/FCO_WW{week}/{qdf}'
    _status(f'Creating directory: {work_dir}', 'step')
    s.send(f'mkdir -p {work_dir} && cd {work_dir}')
    with _guard(f'crear/entrar a {work_dir}'):
        s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)

    # Copy required files for MLC from FCO_Scripts
    _status('Copying mlc and datapattern from ~/FCO_Scripts ...', 'step')
    for f in ['mlc', 'datapattern_halfA_half5.txt']:
        s.send(f'cp ~/FCO_Scripts/{f} .')
        with _guard(f'copy {f} - verify it exists in ~/FCO_Scripts/'):
            s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
        _status(f'  {f} copied', 'info')
    s.send('chmod +x mlc')
    with _guard('chmod mlc'):
        s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)

    _pause('Setup ready — validate the directory in Raritan and press any key to start tests...')

    _status(f'Directory ready: {work_dir}', 'ok')
    return work_dir


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
    """Runs rocket cpu and iax (dsa goes at the end with a special sequence)."""
    results = {}
    for cmd, label in ROCKET_CMDS:
        results[label] = _run_rocket_cmd(s, cmd, label)
        _pause(f'Rocket {label} {results[label]} — press any key to continue...')
    return results


def run_rocket_dsa(s: SVOSSession) -> str:
    """
    Runs rocket dram,dsa with the special preceding sequence:
    killmax -> unmountsv -> rmmodsvos2 -> mountsv -> rocket dram,dsa
    """
    _status('Preparing Rocket DSA: killmax -> unmountsv -> rmmodsvos2 -> mountsv...', 'step')
    for cmd in ['killmax', 'unmountsv', 'rmmodsvos2', 'mountsv']:
        _status(f'  Running {cmd}...', 'info')
        s.send(cmd)
        t = MOUNTSV_TIMEOUT if cmd == 'mountsv' else CMD_TIMEOUT
        with _guard(f'{cmd}'):
            s.read_until(SVOS_PROMPT, timeout=t)
    cmd, label = ROCKET_DSA_CMD
    result = _run_rocket_cmd(s, cmd, label)
    _pause(f'Rocket DSA {result} — press any key to continue...')
    return result


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
    try:
        _, buf = s.read_until_any([b'PASS', b'pass', b'FAIL', b'fail',
                                    SVOS_PROMPT], timeout=SOLAR_TIMEOUT)
        result = 'PASS' if (b'PASS' in buf or b'pass' in buf) else 'FAIL'
    except TimeoutError:
        result = 'FAIL'
        _status('Solar: TIMEOUT', 'fail')
    try:
        s.read_until(SVOS_PROMPT, timeout=120)
    except TimeoutError:
        pass
    _status(f'Solar: {result}', 'ok' if result == 'PASS' else 'fail')
    _pause(f'Solar {result} — press any key to continue...')
    return result


def run_parser(s: SVOSSession):
    """Replicates parser() from the bash script: grep PASS/fail in all .txt files."""
    _status('Running parser (grep on *.txt)...', 'step')
    s.send('grep -r "PASS" *.txt >> output.log 2>/dev/null; grep -r "success" *.txt >> output.log 2>/dev/null; grep -r "fail" *.txt >> output.log 2>/dev/null')
    with _guard('parser grep'):
        s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
    s.send('mkdir -p output && mv output.log output/ 2>/dev/null; cat output/output.log')
    with _guard('move output.log'):
        s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
    _status('Parser completed. Results in output/output.log', 'ok')


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
                     log_dir: Path, timings: dict = None, content=None):
    """Generates fco_result_{qdf}.txt in the script folder and in logs/."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts      = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    overall = 'PASS' if all(v == 'PASS' for v in results.values() if v != 'SKIPPED') else 'FAIL'

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
        'mlc',
        'solar',
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
        f'ULT       : {ult0}',
        f'QDF       : {qdf}',
        f'IFWI      : {ifwi}',
        f'Selected  : {selected_str}',
        f'Overall   : {overall}',
        '',
        f'  {"Content":<{NAME_W}} {"Command":<{CMD_W}}   Result',
        f'  {"-"*NAME_W} {"-"*CMD_W}   ------',
    ] + _content_rows()

    content = '\n'.join(lines)

    if timings:
        TIMING_LABELS = {
            'overwrite_wait': 'Overwrite wait',
            'boot':           'Boot SVOS',
            'supercollider':  'SuperCollider',
            'rocket_cpu_iax': 'Rocket cpu+iax',
            'memicals':       'Memicals',
            'mlc':            'MLC',
            'solar':          'Solar',
            'rocket_dsa':     'Rocket DSA',
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

    # In the script folder (easy to find)
    local_path = BASE_DIR / f'fco_result_{qdf}.txt'
    local_path.write_text(content, encoding='utf-8')

    # In logs/ with timestamp (history)
    archive_path = log_dir / f'FCO_WW{week}_{qdf}_{ts}.txt'
    archive_path.write_text(content, encoding='utf-8')

    logging.info(f'Log saved: {local_path}')
    return local_path, overall, content


def write_summary_log(week: str, ult0: str, ifwi: str, all_results: list, log_dir: Path):
    """Generates FCO_SUMMARY_WW{week}.txt with the results for all QDFs."""
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
        f'ULT       : {ult0}',
        f'IFWI      : {ifwi}',
        f'Overall   : {overall}',
        f'QDFs      : {len(all_results)}  '
        f'(PASS: {sum(1 for r in all_results if r["overall"] in ("PASS","RETRY_PASS"))} / '
        f'FAIL: {sum(1 for r in all_results if r["overall"] not in ("PASS","RETRY_PASS"))})',
        '',
        f'  {"QDF":<{QDF_W}} {"Result":<{OV_W}}{hdr_dur} Log',
        f'  {"-"*QDF_W} {"-"*OV_W}{sep_dur} ---',
    ] + rows

    content = '\n'.join(lines)

    # Embed the full log for each QDF at the end of the summary
    divider = '\n' + '=' * 70 + '\n'
    for r in all_results:
        rc = r.get('result_content')
        if rc:
            content += divider + rc

    local_path   = BASE_DIR / f'fco_summary_WW{week}.txt'
    archive_path = log_dir  / f'FCO_SUMMARY_WW{week}_{ts}.txt'
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
    print(f'Enter the QDFs{label_str} separados por coma.')
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
    print()
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


def _has_svos_content(content) -> bool:
    """
    Checks if there is any SVOS content to run (not just CentOS boot).
    Returns True if there's at least one test other than centos_boot.
    content=None means full content (True).
    """
    if content is None:
        return True
    return any(test in content for test in CONTENT_TESTS if test != 'centos_boot')


def run_centos_boot(s: SVOSSession, mode: int, qdf: str, ult0: str, soc: str = 'x4', 
                     kwargs: dict = None, is_retry: bool = False) -> str:
    """
    Boots CentOS as part of the QDF content workflow.
    For modes with pysv overwrite (1/3/4): triggers overwrite + reboot + boot CentOS.
    For mode 2 (fused): sends reboot command + boots CentOS.
    
    *** IMPORTANT FOR MODE 2 (Fused unit without overwrite) ***
    The SVOS 'reboot' command may fail. If reboot fails:
      1. The system will timeout waiting for BIOS
      2. You MUST run a power cycle using Python SV
      3. Example: sv.pwr.pwrgood.cycle() or power_cycle() command
    Without a power cycle, CentOS cannot boot successfully.
    
    Returns 'PASS' or 'FAIL'.
    """
    _status(f'Running CentOS boot (reboot + \\efi\\boot\\BootCentosDMR.efi + login)...', 'step')
    
    try:
        # Reboot/overwrite preparation depends on mode and fused status
        if mode == 2:
            # Fused without pysv: just reboot
            _status('Fused path: sending reboot command...', 'info')
            if not is_retry:
                print()
                print('  [NOTE-MODE2] Reboot might fail in SVOS.')
                print('  If it does, run POWER CYCLE in pysv:')
                print('    sv.pwr.pwrgood.cycle() or equivalent power_cycle() command')
                print()
            try:
                s.send('reboot')
                time.sleep(1)
            except Exception as e:
                _status(f'Could not send reboot: {e}. Continuing with BIOS wait...', 'warn')
        else:
            # Modes with pysv (1, 3, 4): request one overwrite before CentOS
            signal_prefix = f'{qdf}_centos' if is_retry else qdf
            _status('pysv path: requesting final overwrite for CentOS boot...', 'info')
            
            # Clean signals
            for name in [f'{signal_prefix}_sv_done.signal', f'{signal_prefix}_svos_done.signal']:
                sig = SIGNAL_DIR / name
                if sig.exists():
                    sig.unlink()
            
            # Write qdf_list.json with signal prefix (so sv_automation knows this is for CentOS)
            entry = {'qdf': qdf, 'ult0': ult0, 'soc': soc}
            if kwargs:
                entry['kwargs'] = kwargs
            with open(QDF_LIST_FILE, 'w', encoding='utf-8') as f:
                json.dump([entry], f, indent=2)
            
            print()
            print('=' * 60)
            print('  CentOS Boot — Requesting Overwrite')
            print('=' * 60)
            print(f'  In your pysv session, run sv_automation.run_qdf_list(itp, sv, bs_wrap)')
            print(f'  Waiting for overwrite signal...')
            print()
            _pause('TEST MODE: Press any key to continue (sv_automation must run normally; waiting real signal)...')
            
            # Wait for sv_done signal
            sv_done = SIGNAL_DIR / f'{signal_prefix}_sv_done.signal'
            _wait_for_file(sv_done)
            
            if sv_done.read_text().strip() == 'error':
                _status('sv_automation reported error in overwrite.', 'fail')
                return 'FAIL'
            
            _status('Overwrite completed. Ready for CentOS boot.', 'ok')
            
            # Write svos_done so sv_automation continues
            (SIGNAL_DIR / f'{signal_prefix}_svos_done.signal').write_text('done\n')
        
        # Boot CentOS
        boot_centos(s)
        _status('CentOS boot successful.', 'ok')
        _pause('CentOS boot OK — validate and press any key to continue...')
        return 'PASS'
        
    except Exception as e:
        _status(f'CentOS boot FAILED: {e}', 'fail')
        logging.error(f'CentOS boot failed for {qdf}: {e}', exc_info=True)
        return 'FAIL'


# ---------------------------------------------------------------------------
# Content configuration per QDF
# ---------------------------------------------------------------------------

def _should_run(content, test_name):
    """Returns True if the test should run. content=None means full content."""
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
        ('mlc',           'MLC'),
        ('solar',         'Solar'),
        ('centos_boot',   'CentOS Boot (root/root + ifconfig)'),
    ]

    print()
    resp = input('Full content for ALL QDFs? (y/n): ').strip().lower()
    if resp in ('s', 'y'):
        for item in qdf_list:
            item['content'] = None
        print()
        return

    print()
    for item in qdf_list:
        qdf = item['qdf']
        print(f'  --- {qdf} ---')
        resp_full = input(f'  Full content for {qdf}? (y/n): ').strip().lower()
        if resp_full in ('s', 'y'):
            item['content'] = None
            print(f'  {qdf}: Full content')
            print()
            continue

        selected = []
        while not selected:
            selected = []
            for key, label in _LABELS:
                r = input(f'    Run {label}? (y/n): ').strip().lower()
                if r in ('s', 'y'):
                    selected.append(key)
            if not selected:
                print(f'  [!!] Select at least one test for {qdf}.')

        item['content'] = selected
        selected_display = ', '.join(d for k, d in _LABELS if k in selected)
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
        print(f'  Fused QDF:  {fused_qdf}  ULT={cfg.get("fused_ult0")}  SOC={cfg.get("fused_soc")}')
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
    boot_svos(s)
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
        rocket_res = _run_safe('Rocket cpu/iax', run_rocket, s, _tkey='rocket_cpu_iax')
        if isinstance(rocket_res, dict):
            results.update(rocket_res)
        else:
            for _, label in ROCKET_CMDS:
                results[label] = 'FAIL'
    else:
        for _, label in ROCKET_CMDS:
            results[label] = 'SKIPPED'

    results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals')
                           if _should_run(content, 'memicals') else 'SKIPPED')
    results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc')
                           if _should_run(content, 'mlc')      else 'SKIPPED')
    results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar')
                           if _should_run(content, 'solar')    else 'SKIPPED')

    if _should_run(content, 'rocket'):
        results['rocket_dram_dsa'] = _run_safe('Rocket DSA', run_rocket_dsa, s, _tkey='rocket_dsa')
    else:
        results['rocket_dram_dsa'] = 'SKIPPED'

    try:
        run_parser(s)
    except Exception as e:
        _status(f'Parser failed: {e}', 'fail')

    timings = _timings.get(qdf) if CRONOS_MODE else None
    log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, LOG_DIR,
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

def _run_main_loop(s: SVOSSession, qdf_list: list, week: str, ult0: str, ifwi: str, mode: int) -> list:
    """Executes the SV signals + SVOS boot + tests loop with retry. Returns all_results."""
    all_results  = []
    retry_needed = []

    for i, item in enumerate(qdf_list):
        qdf  = item['qdf']
        ult0 = item['ult0']

        print(f'\n{"="*60}')
        _status(f'QDF {i+1}/{len(qdf_list)}: {qdf}', 'step')
        print(f'{"="*60}')

        try:
            sv_done = SIGNAL_DIR / f'{qdf}_sv_done.signal'
            _status(f'Waiting for SV to complete the fuse overwrite of {qdf}...', 'wait')
            t0_ow = time.time()
            wait_for_signal(sv_done)
            if CRONOS_MODE:
                _timings.setdefault(qdf, {})['overwrite_wait'] = time.time() - t0_ow
            _status(f'Fuse overwrite of {qdf} completed. Starting SVOS...', 'ok')

            t0_boot = time.time()
            boot_svos(s)
            
            content = item.get('content')
            has_svos_tests = _has_svos_content(content)
            
            if has_svos_tests:
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
                rocket_res = _run_safe('Rocket cpu/iax', run_rocket, s, _tkey='rocket_cpu_iax')
                if isinstance(rocket_res, dict):
                    results.update(rocket_res)
                else:
                    for _, label in ROCKET_CMDS:
                        results[label] = 'FAIL'
            else:
                for _, label in ROCKET_CMDS:
                    results[label] = 'SKIPPED'

            results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals')
                                   if _should_run(content, 'memicals') else 'SKIPPED')
            results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc')
                                   if _should_run(content, 'mlc')      else 'SKIPPED')
            results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar')
                                   if _should_run(content, 'solar')    else 'SKIPPED')

            if _should_run(content, 'rocket'):
                results['rocket_dram_dsa'] = _run_safe('Rocket DSA', run_rocket_dsa, s,
                                                       _tkey='rocket_dsa')
            else:
                results['rocket_dram_dsa'] = 'SKIPPED'

            if has_svos_tests:
                try:
                    run_parser(s)
                except Exception as e:
                    _status(f'Parser failed: {e}', 'fail')

            if _should_run(content, 'centos_boot'):
                t0_centos = time.time()
                centos_result = run_centos_boot(s, mode, qdf, ult0, item.get('soc', 'x4'),
                                                item.get('kwargs'), is_retry=False)
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['centos_boot'] = time.time() - t0_centos
                results['centos_boot'] = centos_result
            else:
                results['centos_boot'] = 'SKIPPED'

            log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, LOG_DIR,
                                                  timings=_timings.get(qdf) if CRONOS_MODE else None,
                                                  content=content)

            try:
                summary_lines = [f'FCO WW{week} {qdf} - Overall: {overall}'] + \
                                [f'{k}: {v}' for k, v in results.items()]
                s.send(f'echo "{chr(10).join(summary_lines)}" > fco_result.txt')
                s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
            except Exception as e:
                _status(f'Could not write fco_result.txt in SVOS: {e}', 'fail')

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
                svos_done.write_text('done\n')
                _status(f'Signal written for SV: {svos_done.name}', 'info')
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
                wait_for_signal(SIGNAL_DIR / f'{qdf}_retry_sv_done.signal')
                _status(f'Retry overwrite of {qdf} completed. Booting SVOS...', 'ok')

                t0_boot_r = time.time()
                boot_svos(s)
                setup_fco_dir(s, qdf, week)
                if CRONOS_MODE:
                    _timings.setdefault(qdf, {})['boot'] = time.time() - t0_boot_r

                content_r = retry_item.get('content')
                results = {}

                def _run_safe_r(name, fn, *args, _tkey=None):
                    t0 = time.time()
                    try:
                        result = fn(*args)
                    except Exception as e:
                        _status(f'{name} FAILED: {e}', 'fail')
                        logging.error(f'[RETRY] {name} failed for {qdf}: {e}', exc_info=True)
                        result = 'FAIL'
                    if CRONOS_MODE and _tkey:
                        _timings.setdefault(qdf, {})[_tkey] = time.time() - t0
                    return result

                if _should_run(content_r, 'supercollider'):
                    results['supercollider'] = _run_safe_r('SuperCollider', run_supercollider, s,
                                                           _tkey='supercollider')
                else:
                    results['supercollider'] = 'SKIPPED'

                if _should_run(content_r, 'rocket'):
                    rocket_res = _run_safe_r('Rocket cpu/iax', run_rocket, s, _tkey='rocket_cpu_iax')
                    if isinstance(rocket_res, dict):
                        results.update(rocket_res)
                    else:
                        for _, label in ROCKET_CMDS:
                            results[label] = 'FAIL'
                else:
                    for _, label in ROCKET_CMDS:
                        results[label] = 'SKIPPED'

                results['memicals'] = (_run_safe_r('Memicals', run_memicals, s, _tkey='memicals')
                                       if _should_run(content_r, 'memicals') else 'SKIPPED')
                results['mlc']      = (_run_safe_r('MLC', run_mlc, s, _tkey='mlc')
                                       if _should_run(content_r, 'mlc')      else 'SKIPPED')
                results['solar']    = (_run_safe_r('Solar', run_solar, s, _tkey='solar')
                                       if _should_run(content_r, 'solar')    else 'SKIPPED')

                if _should_run(content_r, 'rocket'):
                    results['rocket_dram_dsa'] = _run_safe_r('Rocket DSA', run_rocket_dsa, s,
                                                             _tkey='rocket_dsa')
                else:
                    results['rocket_dram_dsa'] = 'SKIPPED'

                try:
                    run_parser(s)
                except Exception as e:
                    _status(f'Parser failed in retry: {e}', 'fail')

                log_path, overall, result_content = write_result_log(qdf, week, ult0, ifwi, results, LOG_DIR,
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
                   content=None, mode=2, soc='x4', kwargs=None) -> tuple:
    """
    Runs tests on a fused unit (Modes 2 & 3, Phase A).
    No signal coordination with SV — the unit is already fused.
    Returns: (log_path, overall, result_content)
    """
    boot_svos(s)
    
    has_svos_tests = _has_svos_content(content)
    if has_svos_tests:
        setup_fco_dir(s, qdf, week)

    results = {}

    def _run_safe(name, fn, *args, _tkey=None):
        t0 = time.time()
        try:
            result = fn(*args)
        except Exception as e:
            _status(f'{name} FAILED: {e}', 'fail')
            logging.error(f'{name} failed for fused QDF {qdf}: {e}', exc_info=True)
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
        rocket_res = _run_safe('Rocket cpu/iax', run_rocket, s, _tkey='rocket_cpu_iax')
        if isinstance(rocket_res, dict):
            results.update(rocket_res)
        else:
            for _, label in ROCKET_CMDS:
                results[label] = 'FAIL'
    else:
        for _, label in ROCKET_CMDS:
            results[label] = 'SKIPPED'

    results['memicals'] = (_run_safe('Memicals', run_memicals, s, _tkey='memicals')
                           if _should_run(content, 'memicals') else 'SKIPPED')
    results['mlc']      = (_run_safe('MLC', run_mlc, s, _tkey='mlc')
                           if _should_run(content, 'mlc')      else 'SKIPPED')
    results['solar']    = (_run_safe('Solar', run_solar, s, _tkey='solar')
                           if _should_run(content, 'solar')    else 'SKIPPED')

    if _should_run(content, 'rocket'):
        results['rocket_dram_dsa'] = _run_safe('Rocket DSA', run_rocket_dsa, s,
                                               _tkey='rocket_dsa')
    else:
        results['rocket_dram_dsa'] = 'SKIPPED'

    if has_svos_tests:
        try:
            run_parser(s)
        except Exception as e:
            _status(f'Parser failed: {e}', 'fail')

    if _should_run(content, 'centos_boot'):
        t0_centos = time.time()
        centos_result = run_centos_boot(s, mode, qdf, ult0, soc, kwargs, is_retry=False)
        if CRONOS_MODE:
            _timings.setdefault(qdf, {})['centos_boot'] = time.time() - t0_centos
        results['centos_boot'] = centos_result
    else:
        results['centos_boot'] = 'SKIPPED'

    log_path, overall, result_content = write_result_log(
        qdf, week, ult0, ifwi, results, LOG_DIR,
        timings=_timings.get(qdf) if CRONOS_MODE else None,
        content=content,
    )

    if overall == 'FAIL':
        _status(f'Fused QDF {qdf}: FAILED — see {log_path}', 'fail')
        _alert_popup_async(f'FCO FAILED — {qdf}',
                           f'One or more content items failed.\nSee: {log_path}')
    else:
        _status(f'Fused QDF {qdf}: PASS', 'ok')

    return log_path, overall, result_content


# ---------------------------------------------------------------------------
# Extra timeouts for SVOS utilities
# ---------------------------------------------------------------------------

OSVOSUPDATE_TIMEOUT = 900   # osvosupdate -v                        15 min
SVOSINFO_TIMEOUT    = 60    # svosinfo                               1 min


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


def _do_sv_overwrite_wait(qdf: str, ult0: str, soc: str = 'x4', kwargs=None):
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
    for name in [f'{qdf}_sv_done.signal', f'{qdf}_svos_done.signal']:
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
    print()
    print('=' * 60)
    print('  ACTION REQUIRED — Fuse Overwrite')
    print('=' * 60)
    print()
    print('  In your pysv session, run:')
    print()
    print('      import sys')
    print(f'      sys.path.insert(0, r\'{BASE_DIR}\')')
    print('      import sv_automation')
    print('      sv_automation.run_qdf_list(itp, sv, bs_wrap)')
    print()
    print(f'  Waiting for overwrite completion signal ({qdf}_sv_done.signal)...')
    print()
    _pause('TEST MODE: Press a key to continue (sv_automation must run normally; waiting real signal)...')

    # Wait for the sv_done signal
    sv_done = SIGNAL_DIR / f'{qdf}_sv_done.signal'
    _wait_for_file(sv_done)

    content = sv_done.read_text(encoding='utf-8').strip()
    if content == 'error':
        raise FCOStepError(f'sv_automation reporto error en el overwrite de {qdf}. '
                           f'Revisa la consola de pysv.')
    _status(f'Overwrite of {qdf} completed by sv_automation.', 'ok')

    # Write svos_done so sv_automation does not remain blocked waiting
    svos_done = SIGNAL_DIR / f'{qdf}_svos_done.signal'
    svos_done.write_text('done\n')
    _status(f'svos_done signal written for {qdf}.', 'info')





# ---------------------------------------------------------------------------
# SVOS utilities — tools menu
# ---------------------------------------------------------------------------

def _ask_tool_menu() -> str:
    """
    Shows the main tools menu before the FCO flow.
    Returns: 'fco' | 'boot' | 'update' | 'centos_direct'
    Prefix 't' activates TEST_MODE (e.g. t1, t2, t3, t4).
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
    print()
    print('  Prefix t for TEST MODE (e.g. t1, t2, t3, t4)')
    print()
    _map = {'1': 'fco', '2': 'boot', '3': 'update', '4': 'centos_direct'}
    while True:
        raw = input('Tool (1-4): ').strip().lower()
        if raw.startswith('t') and raw[1:] in _map:
            TEST_MODE = True
            print('  [TEST MODE activated]')
            return _map[raw[1:]]
        if raw in _map:
            return _map[raw]
        print('  [!!] Enter 1, 2, 3 or 4.')


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
    Tool 2: Boots SVOS and leaves the session at root@sut:/>.
    If the unit is not fused, it coordinates the overwrite with sv_automation (pysv) first.
    """
    print()
    print('=' * 60)
    print('  BOOT SVOS')
    print('=' * 60)

    fused = _ask_fused()

    if not fused:
        qdfs, ult0, soc, kwargs = _ask_qdf_params()
        if not qdfs:
            print('[!!] ERROR: no QDF entered.')
            return
        if len(qdfs) > 1:
            print('[!] For Boot SVOS only the first QDF is used for the overwrite.')
        qdf = qdfs[0]
        try:
            _do_sv_overwrite_wait(qdf, ult0, soc, kwargs)
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            return

    _status(f'Abriendo {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        _pause('Ready to start SVOS boot. Press a key...')
        boot_svos(s, do_mountsv=False)
        _status('SVOS ready — root@sut:/> active.', 'ok')
        _alert_popup_async('Boot SVOS OK',
                           'SVOS booteo correctamente y quedo activo en root@sut:/>.')
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

    fused = _ask_fused()
    overwrite_qdf = None

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

    # --- Preliminary step: overwrite if not fused ---
    if overwrite_qdf:
        try:
            _do_sv_overwrite_wait(overwrite_qdf, ult0, soc, kwargs)
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            return

    _status(f'Abriendo {com_port}...', 'step')
    s = _open_serial(com_port)
    try:
        total_steps = 5
        step = 1

        # 1. Boot SVOS
        _pause(f'Step {step}/{total_steps} — Boot SVOS. Press a key...')
        _status(f'Step {step}/{total_steps} — Boot SVOS...', 'step')
        boot_svos(s, do_mountsv=False)
        _status('SVOS ready.', 'ok')
        step += 1

        # 2. osvsetrelease
        _pause(f'Step {step}/{total_steps} — osvsetrelease. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: osvsetrelease -r {release} -u -n {patch}', 'step')
        s.send(f'osvsetrelease -r {release} -u -n {patch}')
        with _guard(f'osvsetrelease -r {release} -u -n {patch}'):
            s.read_until(SVOS_PROMPT, timeout=CMD_TIMEOUT)
        _status('osvsetrelease completed.', 'ok')
        step += 1

        # 3. osvosupdate -v
        _pause(f'Step {step}/{total_steps} — osvosupdate. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: osvosupdate -v (may take ~10 min)...', 'step')
        s.send('osvosupdate -v')
        with _guard('osvosupdate -v'):
            s.read_until(SVOS_PROMPT, timeout=OSVOSUPDATE_TIMEOUT)
        _status('osvosupdate completed.', 'ok')
        step += 1

        # 4. umountsv; mountsv — with recovery if it hangs
        _pause(f'Step {step}/{total_steps} — umountsv; mountsv. Press a key...')
        _status(f'Step {step}/{total_steps} — Running: umountsv; mountsv...', 'step')
        s.send('umountsv; mountsv')
        mountsv_ok = False
        _UPDATE_MOUNTSV_TIMEOUT = 900  # 15 min (FCO usa 30 min)
        try:
            s.read_until(SVOS_PROMPT, timeout=_UPDATE_MOUNTSV_TIMEOUT)
            mountsv_ok = True
            _status('mountsv completed.', 'ok')
        except TimeoutError:
            _status(f'mountsv did not respond within {_UPDATE_MOUNTSV_TIMEOUT//60} min — iniciando recovery...', 'warn')
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
                boot_svos(s, do_mountsv=False)
                _status('SVOS back after reboot.', 'ok')
            mountsv_ok = True
        step += 1

        # 5. svosinfo + verification
        _pause(f'Step {step}/{total_steps} — svosinfo. Press a key...')
        _status(f'Step {step}/{total_steps} — Verifying with svosinfo...', 'step')
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

    fused = _ask_fused()
    did_wrapper = False

    if not fused:
        qdfs, ult0, soc, kwargs = _ask_qdf_params()
        if not qdfs:
            print('[!!] ERROR: no QDF entered.')
            return
        if len(qdfs) > 1:
            print('[!] For Boot CentOS only the first QDF is used for wrapper overwrite.')
        qdf = qdfs[0]
        try:
            _do_sv_overwrite_wait(qdf, ult0, soc, kwargs)
            did_wrapper = True
        except Exception as e:
            _status(f'Error in overwrite: {e}', 'fail')
            _alert_popup('Overwrite FAILED', str(e))
            return

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

        boot_centos(s)
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

    logging.info('=== FCO SVOS Automation ===')

    global TEST_MODE

    # ---- Main tools menu ----
    tool = _ask_tool_menu()

    if tool in ('boot', 'update', 'centos_direct'):
        # Tools that only need COM port (no week or FCO mode)
        print()
        com_port = input('COM port (e.g.: COM9 or just 9): ').strip()
        if not com_port.upper().startswith('COM'):
            com_port = 'COM' + com_port

        if tool == 'boot':
            run_boot_svos_only(com_port)
        elif tool == 'update':
            run_update_svos(com_port)
        else:  # tool == 'centos_direct'
            run_boot_centos_direct(com_port)
        if tool == 'update':
            input('\nPress ENTER to close...')
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
    summary_ult0  = 'N/A'
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

            if mode == 4:
                print()
                _status('Mode 4: Fused unit — ignoring the current boot.', 'info')
                _status('        Waiting for SV to start the overwrite...', 'info')

            summary_ult0 = ult0
            all_results  = _run_main_loop(s, qdf_list, week, ult0, ifwi, mode)

        elif mode == 2:
            if use_last:
                qdf           = _cfg_last['fused_qdf']
                ult0          = _cfg_last['fused_ult0']
                soc           = _cfg_last.get('fused_soc', 'x4')
                fused_content = _cfg_last.get('fused_content')
                print(f'\n  Fused QDF: {qdf}  ULT={ult0}  SOC={soc}')
                content_str = 'full' if fused_content is None else ', '.join(fused_content)
                print(f'  Content: {content_str}')
            else:
                qdfs, ult0, soc, kwargs = _ask_qdf_params('(fused QDF)')
                if not qdfs:
                    print('\n[!!] ERROR: no QDF entered.')
                    input('\nPress ENTER to close...')
                    sys.exit(1)
                qdf = qdfs[0]
                _fused_list = [{'qdf': qdf}]
                _ask_content_config(_fused_list)
                fused_content = _fused_list[0].get('content')
                _save_last_config({
                    'mode': mode, 'test_mode': TEST_MODE,
                    'com_port': com_port, 'week': week,
                    'fused_qdf': qdf, 'fused_ult0': ult0, 'fused_soc': soc,
                    'fused_kwargs': kwargs, 'fused_content': fused_content,
                })

            logging.info(f'COM: {com_port} | Mode: 2 | Week: WW{week} | '
                         f'ULT: {ult0} | Fused QDF: {qdf}')

            s = _open_serial(com_port)

            print(f'\n{"="*60}')
            _status(f'Modo 2 — Testeando Fused QDF: {qdf}', 'step')
            print(f'{"="*60}')
            print()
            print('  [INFO] Mode 2: Fused unit only (no pysv overwrite)')
            print('  If CentOS boot is selected, reboot is needed:')
            print('    - SVOS reboot may fail')
            print('    - If it fails, run power cycle in pysv')
            print('    - Without power cycle, CentOS cannot boot')
            print()

            log_path, overall, result_content = run_fused_test(s, qdf, ult0, week, ifwi,
                                                               content=fused_content, mode=mode)
            summary_ult0 = ult0
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
                print('\n[Phase A] Enter the fused QDF to test first:')
                qdfs_f, ult0_f, soc_f, _ = _ask_qdf_params('(fused QDF)')
                if not qdfs_f:
                    print('\n[!!] ERROR: no fused QDF entered.')
                    input('\nPress ENTER to close...')
                    sys.exit(1)
                qdf_fused = qdfs_f[0]

                # Content configuration for the fused QDF
                print('\n[Phase A Content] Configure the tests for the fused QDF:')
                _fused_list_m3 = [{'qdf': qdf_fused}]
                _ask_content_config(_fused_list_m3)
                fused_content_m3 = _fused_list_m3[0].get('content')

                # PHASE B: QDFs to overwrite
                print('\n[Phase B] Enter the additional QDFs to overwrite after the fused test:')
                qdfs_ow, ult0_ow, soc_ow, kwargs_ow = _ask_qdf_params('(QDFs to overwrite)')
                if not qdfs_ow:
                    print('\n[!!] ERROR: no QDFs to overwrite entered.')
                    input('\nPress ENTER to close...')
                    sys.exit(1)

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

            # PHASE A: test the fused QDF
            print(f'\n{"="*60}')
            _status(f'PHASE A — Testing fused QDF: {qdf_fused}', 'step')
            print(f'{"="*60}')

            log_path_f, overall_f, result_content_f = run_fused_test(s, qdf_fused, ult0_f, week, ifwi,
                                                                      content=fused_content_m3, mode=mode,
                                                                      soc=soc_f, kwargs=None)
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
            summary_path = write_summary_log(week, summary_ult0, ifwi, all_results, LOG_DIR)
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
    input('\nPress ENTER to close...')


if __name__ == '__main__':
    _self_update()
    try:
        main()
    except Exception as e:
        print(f'\n[!!] ERROR at startup: {e}')
        import traceback
        traceback.print_exc()
        input('\nPress ENTER to close...')

