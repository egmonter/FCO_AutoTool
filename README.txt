==============================================
  FCO AUTOMATION — HOW TO USE
==============================================

FILES
-----
FCO_AutoTool/
├── config.json            <- EDIT: fixed wrapper parameters (proj, stepping, etc.)
├── timeouts_config.json   <- EDIT: timeout values in seconds (BIOS/SVOS/Rocket/etc.)
├── qdf_list.json          <- automatically generated when running FCO_AutoTool.py
├── sv_automation.py       <- run INSIDE the SV session
├── FCO_AutoTool.py        <- run in a separate Python window
├── requirements.txt
├── signals/               <- SV<->SVOS coordination files (auto-generated)
└── logs/                  <- per-QDF results + summary (auto-generated)


INSTALLATION (first time only)
----------------------------------
  git clone https://github.com/egmonter/FCO_AutoTool.git
  pip install pyserial


==============================================
  STEP BY STEP
==============================================

STEP 1 — Start FCO_AutoTool.py
------------------------------
Open a SEPARATE CMD/Python window and run:

  cd <FCO_AutoTool_path>
  python FCO_AutoTool.py

At startup it will show the TOOLS MENU:

Before the tools menu, it asks for EXECUTION PROFILE:

  1 - Normal
  2 - Validation (No Kill Time)

Validation (No Kill Time) disables timeout-based kills for all tools
(1 to 5). Use it for validation/debug runs where you do not want
automatic timeout aborts.

  1 - FCO Automation          (fuse + test)
  2 - Boot SVOS only
  3 - Update SVOS             (osvsetrelease + osvosupdate)
  4 - Boot CentOS only
  5 - EFI Timing              (overwrite + time to BIOS/EFI gray screen)
  6 - IA Mode                 (load configuration prepared by the agent)

  Prefix t for TEST MODE (e.g. t1, t2, t3, t4, t5, t6)


Then, depending on the tool selected:

TOOL 1 (FCO Automation):
  a) Execution mode:
       1 - Unit NOT fused   → overwrite + test           <- normal flow
       2 - Fused unit       → test the fused QDF only
       3 - Fused unit       → test fused FIRST, then overwrite other QDFs
       4 - Fused unit       → ignore fused, overwrite other QDFs only

      Prefix 't' for TEST_MODE (e.g. t1, t2, t3, t4)

  b) COM port       → enter COM9, COM10 or COM11 (or just the number)
  c) Week           → enter the number, e.g.: 17 (or WW17)

  d) QDFs and parameters:
       - QDFs separated by commas, e.g.: Q9WK, QABC, QXYZ
       - ULT (same for all), e.g.: 0x415420818A1CA102
       - SOC: x4 or x1
       - Extra args (optional, Python dict), e.g.: {'disable_axon': True}

      Wrapper parameter entry:
        - Choose individual fields (default), or paste a full command:
          bs_wrap.main(qdf='QCQL', ult0='0x41932011C09EA181', proj='DMRUCC', stepping_fs='a0', soc='x4', compute_config='x4', pwrgoodmethod='usb', fused_unit=False, extra_args={'disable_axon': True, 'stepping': 'A0'}, pwrgooddelay=30)
        - The pasted command is parsed as literal Python values; it is not executed.
        - If you provide multiple QDFs, the tool substitutes only `qdf` and asks
          for confirmation whenever a generated command differs from the pasted one.
        - Pasted arguments are used as-is and are not merged with config.json defaults.
        - Full-command mode also accepts another imported alias, such as cfcio.main(...).
          Before starting the queue, place bs_wrap_cfcio_bump.py under the local pysv
          users/mkcummin folder and run in the pysv console:
            import users.mkcummin.bs_wrap_cfcio_bump as cfcio
          The alias must be loaded in the active pysv console; otherwise the request
          fails before hardware operations. Individual-field mode still uses bs_wrap.
          Custom aliases are preserved for overwrite retries and CentOS wrapper requests.

  e) Content to run per QDF:
       - Full SVOS content for all  → answer 'y'
         (this includes only SVOS tests: SuperCollider, Rocket, Memicals, Solar, MLC)
       - Then it asks separately:
           * Run CentOS Boot Check for ALL QDFs? (y/n)
       - Or configure per QDF (select at least one):
           * SuperCollider
           * Rocket (cpu/iax/dsa)
           * Memicals
           * Solar
           * MLC
           * CentOS Boot (optional: reboots system, boots via \\EFI\\centos\\grubx64.efi,
                  login root/root, runs ifconfig check)
                  If the serial console reaches LINUX 8.13 but no login prompt
                  appears, the run is marked as conditional PASS with a note
                  that login could not be completed from serial.
                  This conditional PASS is applied after ~3 minutes waiting
                  for serial login in that state.
                  In that case the summary keeps the boot as PASS, but flags the
                  operator note that serial login was not available.
       - If all SVOS content items are "no", it asks:
           * Run SVOS Boot check? (svosinfo response check)
       - Then it asks CentOS separately:
         * Run CentOS Boot Check?
       - You can run only SVOS check, only CentOS, both, or any content combination.

      TEST MODE note:
        - Prefixing the tool with 't' enables TEST_MODE.
        - At a pause, press 'm' to enter manual serial mode.
        - Manual serial mode supports: continue, /enter, /esc, /ctrlc, /wait <sec>.
        - Any other text is sent directly to the serial console.

TOOL 2 (Boot SVOS only):
  - COM port → same as above
  - Asks if unit is fused
  - If not fused: coordinates with sv_automation for overwrite
  - Boots SVOS and leaves at root@... prompt
  - Keeps the tool window open after boot (Ctrl+C to close)

TOOL 3 (Update SVOS):
  - COM port → same as above
  - Asks if unit is fused
  - If not fused: coordinates with sv_automation for overwrite
  - Boots SVOS, runs osvsetrelease + osvosupdate, verifies with svosinfo

TOOL 4 (Boot CentOS only):
  - COM port → same as above
  - Asks if unit is fused (same flow as Boot SVOS)
  - If unit is NOT fused: coordinates with sv_automation for overwrite first
  - If unit is fused: continues directly to CentOS boot
  - Boots via BIOS -> UEFI -> \\EFI\\centos\\grubx64.efi
  - Validates with login root/root + ifconfig
  - If the serial console reaches LINUX 8.13 but no login prompt appears,
    the run is marked as conditional PASS and notes that serial login could
    not be completed
    (triggered after ~3 minutes in that state)
  - Keeps the tool window open after boot (Ctrl+C to close)

TOOL 6 (IA Mode):
  - No interactive prompts for mode/COM/week/QDFs/content — the configuration
    comes from a JSON file prepared ahead of time (by hand or by an AI agent).
  - The tool detects this machine's hostname and reads:
      I:\engineering\dev\user_links\egmonter\FCO_AutoTool_ENG\<HOSTNAME>\ia_config.json
    The <HOSTNAME> folder is created automatically if it does not exist yet.
  - ia_config.json uses the exact same schema as the normal "last saved
    configuration" (mode, com_port, week, qdf_list with per-QDF content),
    plus an informational "generated_at" timestamp.
  - On launch it prints the file path, the "generated_at" timestamp, and the
    full configuration summary, then asks "Use this IA configuration? (y/n)"
    — same safety confirmation as the normal flow, so you can see at a glance
    whether you are about to run the latest prepared data or a stale file.
  - If no ia_config.json is found for this host, it tells you where it looked
    and exits without attempting a run.
  - Today this has only been exercised with the Mode 1/4 schema (qdf_list
    array); Modes 2/3 schemas should work the same way but are not yet
    validated through IA Mode.
  - Real-time I: drive mirror (any mode, not just IA Mode): whenever this host's
    IA folder is reachable, every run also mirrors, live, to
      I:\engineering\dev\user_links\egmonter\FCO_AutoTool_ENG\<HOSTNAME>\logs\
    the app log, the live serial transcript (Serial Logs\live_<port>.log), and
    the per-QDF/summary reports — the same files/content written locally, just
    duplicated there as they are produced. This is the bridge used to diagnose
    hosts that cannot be reached directly (e.g. no UNC/admin-share access).
    If the I: drive is not reachable, the tool logs a warning and continues
    normally without the mirror (non-fatal).

The script writes qdf_list.json with the entered parameters and then waits
for the SV signal for each QDF. Do NOT close it.

Skip-boot prompt behavior:
  - FCO_AutoTool first probes the serial port for a live SVOS prompt (root@...)
  - It asks "Skip boot (y/n)?" only when SVOS is actually detected
  - If no SVOS prompt is detected, it continues normal boot flow automatically


STEP 2 — Run sv_automation in your SV session
-----------------------------------------------
In your already open SV session, paste this:

  import users.mkcummin.fle_bs_wrapper as bs_wrap
  import sys
  sys.path.insert(0, r'<FCO_AutoTool_path>')
  import sv_automation
  sv_automation.run_qdf_list(itp, sv, bs_wrap)

  (The exact path is automatically updated each time FCO_AutoTool.py runs)

FCO_AutoTool also prints the same pysv commands in-console during overwrite flow
as an ACTION REQUIRED reminder.

sv_automation.py reads qdf_list.json, performs forcereconfig/unlock/refresh, and
runs bs_wrap.main() for each QDF, then writes the coordination signal.

If you are running Mode 2 with CentOS boot selected, use this instead:

  import sys
  sys.path.insert(0, r'<FCO_AutoTool_path>')
  import sv_automation
  sv_automation.run_mode2_centos_monitor()

  (The QDF is auto-detected from qdf_list.json generated by Mode 2)

The Mode 2 monitor waits for {QDF}_svos_done.signal and then handles the
CentOS power cycle automatically. It does not run the overwrite.

TEST_MODE / debug flow:
  - TEST_MODE keeps the normal automation path, but pauses can be used to
    inspect or interact with the live serial session.
  - At a pause, press 'm' to enter manual serial mode.
  - Use manual mode when you want to see the live boot state, send ENTER/ESC,
    interrupt with Ctrl+C, or wait and drain serial output without leaving the tool.


==============================================
  WHAT HAPPENS NEXT (automatic for each QDF)
==============================================

  SV Session                        SVOS (serial via COM port)
  ─────────────────────────────     ──────────────────────────────────────────
  forcereconfig / unlock            waiting for signal...
  sv.refresh()
  bs_wrap.main(QDF, ULT, SOC...)
  writes {QDF}_sv_started.signal →  starts overwrite timer in monitor
  writes {QDF}_sv_done.signal →     detects completion signal
                                    waits for reboot (10s) + flush buffer
                                    looks for BIOS screen (OAKSTREAM/EDKII/UEFI)
                                    → navigates Boot Manager Menu
                                    → navigates UEFI Internal Shell
                                    → waits for EFI Shell prompt
                                    → looks for \\EFI\\debian\\grubx64.efi on FS0/FS1/FS2
                                    → ENTER (ATTENTION prompt)
                                    → waits for root@... prompt
                                    → login (root / svos)
                                    → mountsv
                                    → mkdir /root/FCO/FCO_WW{week}/{QDF}
                                    → copies mlc + datapattern from ~/FCO_Scripts
                                    → SuperCollider    (sc -M 5)
                                    → Rocket cpu       (rocket --hw dram,cpu + rtm)
                                    → Rocket DSA/VTD   (fast path: rocket --hw dram,dsa,vtd)
                                    → Rocket iax       (rocket --hw dram,iax + rtm)
                                    → Memicals         (memic.py -M 15)
                                    → Solar            (/usr/bin/solar/solar.sh)
                                    → MLC              (mlc --loaded_latency -t60)
                                    → Rocket DSA/VTD fallback (only if fast path was FAIL/UNKNOWN):
                                                        killmax→unmountsv→rmmodsvos2
                                                        →mountsv→retry rocket)
                                    → parser (grep PASS/FAIL in *.txt → output.log)
                                    → saves QDF log
  detects {QDF}_svos_done.signal ←  writes {QDF}_svos_done.signal
  waits for SVOS to finish         ready for next QDF
  moves to the next QDF


AUTOMATIC RETRY (BIOS / mountsv / bootscript failure)
------------------------------------------
If during the main loop a QDF fails due to a timeout in BIOS or mountsv, or
due to a bootscript/overwrite error reported by sv_automation, FCO_AutoTool.py
queues it for retry. CentOS validation failures are not sent to the retry
queue; they are reported on that QDF and the flow continues with the next
one. When the main loop finishes:

  1. SVOS writes retry_needed.signal + retry_needed.json
  2. SV detects the signal, performs Power OFF → waits 5 min → Power ON
  3. SV re-runs bs_wrap.main() for each failed QDF
  4. SV writes {QDF}_retry_sv_done.signal
  5. SVOS repeats the boot + tests flow for each QDF in retry
  6. At the end: writes {QDF}_retry_svos_done.signal


==============================================
  FIXED WRAPPER PARAMETERS (config.json)
==============================================

  Edit config.json to change any parameter without touching the code:

  {
    "proj":          "DMRUCC",
    "stepping":      "a0",
    "pwrgoodmethod": "usb",
    "fused_unit":    false,
    "pwrgooddelay":  30
  }

  If config.json does not exist, sv_automation.py uses the values above as defaults.


TIMEOUTS (timeouts_config.json)
-------------------------------

  Edit timeouts_config.json to adjust waits/timeouts without touching code.
  All values are in seconds and must be positive integers.

  Common keys:
    - BIOS_WAIT_TIMEOUT
    - BOOT_TIMEOUT
    - SVOS_LOGIN_TIMEOUT (retry wait after ENTER when SVOS login prompt is delayed)
    - MOUNTSV_TIMEOUT
    - ROCKET_TIMEOUT
    - UPDATE_MOUNTSV_TIMEOUT

  If a key is missing or invalid, FCO_AutoTool.py keeps the built-in default.


==============================================
  CONSOLE MESSAGES
==============================================
  [HH:MM:SS] ...  →  waiting for something
  [HH:MM:SS] >>>  →  running step
  [HH:MM:SS] [OK] →  step completed successfully
  [HH:MM:SS] [!!] →  failure or alert
  [HH:MM:SS] [!]  →  warning (non-blocking)


RESULTS AND LOGS
-----------------
  - Console:  real-time status
  - Popup:    alert if a content item fails (does not block execution)
  - Per QDF:  logs\FCO_WW{week}_{QDF}_{date}.txt
  - Summary:  logs\FCO_SUMMARY_WW{week}_{date}.txt
              (includes IFWI path, ULT, the result of all QDFs,
               and the full log of each one embedded at the end)


==============================================
  IMPORTANT NOTES
==============================================

  - The system must be powered on and reaching BIOS when
    FCO_AutoTool.py detects the SV signal.

  - To CANCEL: Ctrl+C in the FCO_AutoTool.py window

  - To REPEAT only one QDF:
      Run FCO_AutoTool.py again normally with that QDF.
      Previous signals are automatically cleaned at startup.

    - To REPEAT only the boot/tests without a new overwrite (Mode 2):
      Use Mode 2 and start the Mode 2 pysv monitor in Step 2 when CentOS is selected.

  - IFWI is automatically detected from the most recent log in C:\DediLog
    (it looks for the csPath= line) and the full path is recorded in the summary.

  - If BIOS navigation fails, the script retries up to 5 times
    sending ESC before each attempt.

  - Configurable timeouts at the start of FCO_AutoTool.py:
      BIOS_WAIT_TIMEOUT  = 900s  (15 min)
      BOOT_TIMEOUT       = 600s  (10 min)
      MOUNTSV_TIMEOUT    = 1800s (30 min)
      SC_TIMEOUT         = 600s  (10 min)
      ROCKET_TIMEOUT     = 1200s (20 min)
      MEMIC_TIMEOUT      = 2400s (40 min)
      MLC_TIMEOUT        = 2400s (40 min)
      SOLAR_TIMEOUT      = 1200s (20 min)

  - CentOS Boot Check details are documented in Tool 1 content + Tool 4 sections.

  - AUTOMATIC CentOS Boot Coordination (All Modes):
      CentOS boot is handled automatically via pysv coordination.

      Mode 2 (Fused unit — immediate power cycle):
        1. FCO_AutoTool writes {QDF}_svos_done.signal after the fused SVOS flow
        2. In pysv, run:
             import sys
             sys.path.insert(0, r'<FCO_AutoTool_path>')
             import sv_automation
             sv_automation.run_mode2_centos_monitor()
        3. pysv waits for {QDF}_centos_power_cycle.signal
        4. pysv performs power OFF → waits 30s → power ON → waits 15s
        5. pysv confirms: {QDF}_centos_power_cycled.signal
        6. FCO_AutoTool continues CentOS boot via BIOS → UEFI → \\EFI\\centos\\grubx64.efi

      Modes 1/3/4 (Non-fused units — wrapper execution):
        1. FCO_AutoTool sends signal: {QDF}_centos_wrapper.signal (with QDF params)
        2. pysv (idle loop) detects → executes wrapper (bs_wrap.main with params)
        3. pysv confirms: {QDF}_centos_wrapper_done.signal
        4. FCO_AutoTool continues CentOS boot via BIOS → UEFI → \\EFI\\centos\\grubx64.efi

      No manual intervention is needed beyond starting the appropriate pysv
      monitor for the selected mode.

  - SPECIAL CASE: If ONLY CentOS Boot is selected (no SVOS tests):
      - SVOS boot is skipped entirely
      - All SVOS tests are marked SKIPPED (not executed)
      - The tool goes directly to the CentOS boot flow

  - Timeout recovery behavior:
      - If a test times out waiting for the SVOS prompt, the tool attempts an
        automatic shell recovery (Ctrl+C + ENTER) before continuing.
      - This reduces cascaded false FAILs in later steps when the shell is stalled.
      - CentOS boot can return a conditional PASS if the serial console reaches
        LINUX 8.13 but no usable login prompt appears.
      - The conditional PASS cutoff is ~3 minutes of login wait after Linux
        banner detection.
      - A conditional PASS is logged as PASS with a warning/note in the summary.

  - CentOS failure handling:
      - If CentOS login or validation fails, the QDF is marked FAIL and the tool
        continues with the next QDF.
      - There is no interactive [skip/retry/abort] prompt for CentOS validation
        failures in the normal flow.

  - Serial login fallback for CentOS:
      - The normal CentOS flow first tries the configured login credentials,
        then falls back to root/root if the first attempt fails.
      - If the console reaches LINUX 8.13 but never presents a usable login
        prompt, the run is treated as a conditional PASS instead of a hard fail.
