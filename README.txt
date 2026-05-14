==============================================
  FCO AUTOMATION — HOW TO USE
==============================================

FILES
-----
fco_automation/
├── config.json            <- EDIT: fixed wrapper parameters (proj, stepping, etc.)
├── qdf_list.json          <- automatically generated when running svos_automation.py
├── sv_automation.py       <- run INSIDE the SV session
├── svos_automation.py     <- run in a separate Python window
├── requirements.txt
├── signals/               <- SV<->SVOS coordination files (auto-generated)
└── logs/                  <- per-QDF results + summary (auto-generated)


INSTALLATION (first time only)
----------------------------------
  pip install pyserial


==============================================
  STEP BY STEP
==============================================

STEP 1 — Start svos_automation.py
-------------------------------------
Open a SEPARATE CMD/Python window and run:

  cd <fco_automation_path>
  python svos_automation.py

At startup it will prompt for:

  a) Execution mode:
       1 - Unit NOT fused   → overwrite + test           <- normal flow
       2 - Fused unit       → test the fused QDF only
       3 - Fused unit       → test fused FIRST, then overwrite other QDFs
       4 - Fused unit       → ignore fused, overwrite other QDFs only

      Special prefix:
        t1..t4  → activates TEST_MODE (pause between each test for manual validation)

  b) COM port       → enter COM9, COM10 or COM11 (or just the number)
  c) Week           → enter the number, e.g.: 17 (or WW17)

  d) QDFs and parameters:
       - QDFs separated by commas, e.g.: Q9WK, QABC, QXYZ
       - ULT (same for all), e.g.: 0x415420818A1CA102
       - SOC: x4 or x1
       - Extra args (optional, Python dict), e.g.: {'disable_axon': True}

  e) Content to run per QDF:
       - Full content for all  → answer 'y'
       - Or configure per QDF:
           SuperCollider, Rocket (cpu/iax/dsa), Memicals, MLC, Solar

The script writes qdf_list.json with the entered parameters and then waits
for the SV signal for each QDF. Do NOT close it.


STEP 2 — Run sv_automation in your SV session
-----------------------------------------------
In your already open SV session, paste this:

  import users.mkcummin.fle_bs_wrapper as bs_wrap
  import sys
  sys.path.insert(0, r'<fco_automation_path>')
  import sv_automation
  sv_automation.run_qdf_list(itp, sv, bs_wrap)

  (The exact path is automatically updated each time svos_automation.py runs)

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
svos_automation.py queues it for retry. When the main loop finishes:

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
    svos_automation.py detects the SV signal.

  - To CANCEL: Ctrl+C in the svos_automation.py window

  - To REPEAT only one QDF:
      Run svos_automation.py again normally with that QDF.
      Previous signals are automatically cleaned at startup.

  - To REPEAT only the boot/tests without a new overwrite (Mode 2):
      Use Mode 2 — the unit is already fused, sv_automation is not required.

  - IFWI is automatically detected from the most recent log in C:\DediLog
    (it looks for the csPath= line) and the full path is recorded in the summary.

  - If BIOS navigation fails, the script retries up to 5 times
    sending ESC before each attempt.

  - Configurable timeouts at the start of svos_automation.py:
      BIOS_WAIT_TIMEOUT  = 900s  (15 min)
      BOOT_TIMEOUT       = 600s  (10 min)
      MOUNTSV_TIMEOUT    = 1800s (30 min)
      SC_TIMEOUT         = 600s  (10 min)
      ROCKET_TIMEOUT     = 1200s (20 min)
      MEMIC_TIMEOUT      = 2400s (40 min)
      MLC_TIMEOUT        = 2400s (40 min)
      SOLAR_TIMEOUT      = 1200s (20 min)
