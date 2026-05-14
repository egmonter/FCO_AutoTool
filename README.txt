==============================================
  FCO AUTOMATION — HOW TO USE
==============================================

FILES
-----
FCO_AutoTool/
├── config.json            <- EDIT: fixed wrapper parameters (proj, stepping, etc.)
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

  1 - FCO Automation          (fuse + test)
  2 - Boot SVOS only
  3 - Update SVOS             (osvsetrelease + osvosupdate)
  4 - Boot CentOS only

  Prefix t for TEST MODE (e.g. t1, t2, t3, t4)


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

  e) Content to run per QDF:
       - Full content for all  → answer 'y'
       - Or configure per QDF (select at least one):
           * SuperCollider
           * Rocket (cpu/iax/dsa)
           * Memicals
           * MLC
           * Solar
           * CentOS Boot (optional: reboots system, boots via BootCentosDMR.efi,
                          login root/root, runs ifconfig check)
       - If all SVOS content items are "no", it asks:
           * Run SVOS Boot check? (svosinfo response check)
       - Then it asks CentOS separately:
         * Run CentOS Boot Check?
       - You can run only SVOS check, only CentOS, both, or any content combination.

TOOL 2 (Boot SVOS only):
  - COM port → same as above
  - Asks if unit is fused
  - If not fused: coordinates with sv_automation for overwrite
  - Boots SVOS and leaves at root@sut:/> prompt
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
  - Boots via BIOS -> UEFI -> BootCentosDMR.efi
  - Validates with login root/root + ifconfig
  - Keeps the tool window open after boot (Ctrl+C to close)

The script writes qdf_list.json with the entered parameters and then waits
for the SV signal for each QDF. Do NOT close it.


STEP 2 — Run sv_automation in your SV session
-----------------------------------------------
In your already open SV session, paste this:

  import users.mkcummin.fle_bs_wrapper as bs_wrap
  import sys
  sys.path.insert(0, r'<FCO_AutoTool_path>')
  import sv_automation
  sv_automation.run_qdf_list(itp, sv, bs_wrap)

  (The exact path is automatically updated each time FCO_AutoTool.py runs)

sv_automation.py reads qdf_list.json, performs forcereconfig/unlock/refresh, and
runs bs_wrap.main() for each QDF, then writes the coordination signal.


==============================================
  WHAT HAPPENS NEXT (automatic for each QDF)
==============================================

  SV Session                        SVOS (serial via COM port)
  ─────────────────────────────     ──────────────────────────────────────────
  forcereconfig / unlock            waiting for signal...
  sv.refresh()
  bs_wrap.main(QDF, ULT, SOC...)
  writes {QDF}_sv_done.signal →     detects signal
                                    waits for reboot (10s) + flush buffer
                                    looks for BIOS screen (OAKSTREAM/EDKII/UEFI)
                                    → navigates Boot Manager Menu
                                    → navigates UEFI Internal Shell
                                    → waits for EFI Shell prompt
                                    → looks for BootSvosDMR.efi on FS0/FS1/FS2
                                    → ENTER (ATTENTION prompt)
                                    → waits for root@sut:/>
                                    → login (root / svos)
                                    → mountsv
                                    → mkdir /root/FCO/FCO_WW{week}/{QDF}
                                    → copies mlc + datapattern from ~/FCO_Scripts
                                    → SuperCollider    (sc -M 5)
                                    → Rocket cpu       (rocket --hw dram,cpu + rtm)
                                    → Rocket iax       (rocket --hw dram,iax + rtm)
                                    → Memicals         (memic.py -M 15)
                                    → MLC              (mlc --loaded_latency -t60)
                                    → Solar            (/usr/bin/solar/solar.sh)
                                    → Rocket DSA       (killmax→unmountsv→rmmodsvos2
                                                        →mountsv→rocket --hw dram,dsa)
                                    → parser (grep PASS/FAIL in *.txt → output.log)
                                    → saves QDF log
  detects {QDF}_svos_done.signal ←  writes {QDF}_svos_done.signal
  waits for SVOS to finish         ready for next QDF
  moves to the next QDF


AUTOMATIC RETRY (BIOS / mountsv timeout)
------------------------------------------
If during the main loop a QDF fails due to a timeout in BIOS or mountsv,
FCO_AutoTool.py queues it for retry. When the main loop finishes:

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
      Use Mode 2 — the unit is already fused, sv_automation is not required.

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

  - IMPORTANT: Mode 2 CentOS Boot (Automatic Power Cycle):
      In Mode 2 (fused unit), SVOS 'reboot' command does not work.
      Instead, FCO_AutoTool handles this automatically:
        1. FCO_AutoTool sends signal to pysv: {QDF}_centos_power_cycle.signal
        2. pysv immediately executes power cycle (short: 5s OFF → 15s boot wait)
        3. pysv confirms with signal: {QDF}_centos_power_cycled.signal
        4. FCO_AutoTool continues CentOS boot via BIOS → UEFI → BootCentosDMR.efi
      No manual intervention needed for Mode 2 CentOS boot — pysv handles it.

  - SPECIAL CASE: If ONLY CentOS Boot is selected (no SVOS tests):
      - boot_svos() runs but skips setup_fco_dir (no directory/file setup)
      - All SVOS tests are marked SKIPPED (not executed)
      - System reboots directly to CentOS boot
      - Overall result = PASS if CentOS boot successful, FAIL otherwise
