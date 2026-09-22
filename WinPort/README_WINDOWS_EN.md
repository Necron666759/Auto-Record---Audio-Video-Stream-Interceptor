====================================================================
 Auto Record - Audio/Video Stream Interceptor
 User Guide (English) — Windows 10/11 edition
====================================================================

ABOUT
--------------------------------------------------------------------
This program automatically records a Suno track playback (audio) or
YouTube video/audio through a dedicated, isolated Firefox instance:
it launches the browser, opens the page, watches the player and
records the sound (and, for YouTube, optionally the video too),
isolating the capture to just this browser instance rather than the
whole system.

It's controlled through a graphical interface (recorder_gui.py), but
both backends (Suno and YouTube) can also be run directly from the
command line as plain Python scripts with their own flags (--help
lists all of them).

The program can start the browser on its own when you click "Start
recording" - no need to have the main Firefox already open. The
profile is read from %APPDATA%\Mozilla\Firefox\profiles.ini, and the
firefox.exe path is found through the Windows registry (App Paths) or
the standard install folders. On the first successful run, it saves a
dedicated persistent copy of the profile and the firefox.exe path
under %LOCALAPPDATA%\auto_record_suno\, and reuses that copy directly
on every following run. If the saved login session ever goes stale,
the GUI has a "Reset Firefox profile cache" button (or the
--reset-firefox-cache flag on the console scripts) - after that, the
next run will pick up a fresh profile from your main Firefox again
(just open it once and log back in).

This is the Windows edition of the program, ported from the Linux
version. There's essentially no difference from Linux - the
interface, the set of containers, the settings profiles and both
backends (Suno and YouTube) are the same. The only differences are
where the operating system itself requires them (audio isolation,
screen capture, finding Firefox, etc.) - see "WHAT CHANGED
TECHNICALLY" below for details. The Windows edition also has a
"Fallback mode" checkbox (record all system audio without Firefox
isolation), which the Linux version doesn't have - see "KNOWN
LIMITATIONS".


PACKAGE CONTENTS
--------------------------------------------------------------------
  recorder_gui.py           - graphical interface (entry point)
  auto_record_suno.py       - Suno recording logic + all the shared
                               Firefox/audio handling code (also used
                               by the YouTube backend)
  auto_record_youtube.py    - YouTube recording logic (audio/video)
  screen_capture.py         - screen capture for video (ffmpeg gdigrab)
  format_options.py         - list of output formats/containers
  encode_helpers.py         - ffmpeg wrapper helpers
  gui_profiles.py           - saved GUI settings profiles
  i18n.py                   - interface localization (RU/EN)

  win_audio_loopback.py     - pure-ctypes WASAPI audio capture
                               (process isolation + fallback mode)
  win_audio_devices.py      - Windows audio device diagnostics
  win_process.py            - Windows process lookup/termination
  win_stdio.py              - running without a console window
                               (std streams, windowless subprocesses,
                               the STOP command)

  requirements.txt          - no external dependencies (see below)
  build-windows.sh          - builds the portable .exe / .msi /
                               Setup.exe (see "BUILDING A
                               DISTRIBUTABLE" below)


REQUIREMENTS (what must be installed on the system)
--------------------------------------------------------------------
The program does NOT need anything installed via pip - only the
Python standard library. It does need the following, though:

  1. Windows 10 version 2004 (build 19041) or newer, or Windows 11.
     This is a Windows version requirement, not a program one:
     per-process audio isolation (WASAPI Process Loopback Capture)
     was only introduced in that version. On older systems Process
     Loopback isn't available - use fallback mode (see "KNOWN
     LIMITATIONS" below) or upgrade Windows.
  2. Python 3.9+ with the tkinter module (for the GUI). The regular
     installer from python.org ships tkinter together with Python -
     nothing extra to install. If you're running the pre-built
     .exe/.msi/Setup.exe (see "BUILDING A DISTRIBUTABLE") you don't
     need Python on the machine at all - it's already bundled inside.
  3. A genuine (NOT Microsoft Store) Firefox install. The program
     needs the path to firefox.exe, which it looks up through the
     Windows registry (App Paths) and the standard install folders -
     the Store version of Firefox (MSIX) uses a different install
     layout and the binary path may not be found. The regular
     installer from firefox.com works fine.
  4. Free disk space for the automatically downloaded ffmpeg and
     geckodriver (see "WHERE DOWNLOADED FILES ARE SAVED" below) -
     typically up to 150-200 MB.

None of the following need to be installed separately - the program
downloads and caches them on its own on first run (internet is only
needed for this one-time download):

  - geckodriver - from GitHub Releases (github.com/mozilla/geckodriver);
  - ffmpeg - a ready-made Windows build from gyan.dev (essentials).
    If a system ffmpeg is already on PATH and has the needed codecs,
    that one is used and nothing is auto-downloaded.

Unlike the Linux version, you do NOT need: PulseAudio/PipeWire and
its pactl/parec utilities (audio isolation is done through WASAPI,
built into Windows itself), wf-recorder (video capture uses ffmpeg
gdigrab, also nothing to install), or FUSE (the AppImage concept
doesn't apply on Windows - see "BUILDING A DISTRIBUTABLE").


INSTALLATION
--------------------------------------------------------------------
Option A - running from source (needs Python, see above):

  python -m venv venv
  venv\Scripts\activate
  pip install -r requirements.txt

The pip install step doesn't actually install anything (requirements.txt
is empty - the program has no external dependencies, only the
standard library), and venv isn't strictly necessary either, but
these steps are kept as the familiar routine in case dependencies
get added later.

Option B - the pre-built .exe/.msi/Setup.exe (no Python needed at
all): see "BUILDING A DISTRIBUTABLE" below (or use an already-built
file if one was provided separately).

ffmpeg and geckodriver don't need to be installed separately in
either option - the script downloads them on first run.


RUNNING
--------------------------------------------------------------------
Graphical interface:

  python recorder_gui.py

All .py files must live in the same folder (they import each other
by relative path next to the script). A pre-built .exe already
bundles everything inside - it doesn't need the separate .py files.

Or directly from the command line, without the GUI:

  python auto_record_suno.py -d C:\Users\me\Music
  python auto_record_youtube.py -d C:\Users\me\Videos --container mp4


FIREFOX PROFILE CACHE AND GUI SETTINGS PROFILES
--------------------------------------------------------------------
Location (program data, not downloaded from the internet):
  %LOCALAPPDATA%\auto_record_suno\
    firefox_profile\            - persistent copy of the Firefox profile
    firefox_binary_path.txt     - remembered path to firefox.exe

Location (downloaded helper tools; see "WHERE DOWNLOADED FILES ARE
SAVED" below for when this path is actually used):
  %LOCALAPPDATA%\auto_record_suno\codecs\geckodriver\, ...\ffmpeg\

Location (GUI settings profiles - container, destination folder,
URL, interface language, fallback mode, etc.):
  %APPDATA%\auto_record_gui\profiles\<name>.json
  %APPDATA%\auto_record_gui\last_profile.txt   - last opened profile

The very first recording run needs your main Firefox open and logged
in - that's where the program takes the profile and binary path from,
once. Any run after that no longer needs it - the saved copy is used.

If the saved Suno/YouTube login session goes stale (you get logged
out), click "Reset Firefox profile cache" in the GUI, log back in on
your main Firefox, and start a recording again - the cache will be
rebuilt from the fresh profile.


AUDIO DIAGNOSTICS
--------------------------------------------------------------------
The "Check audio devices" button in the GUI (or the
--list-audio-devices flag on either console script) shows: the
Windows version and whether it supports per-process audio isolation,
a list of output/input devices with their status and format, and an
actual test of starting capture in both modes (process isolation and
fallback) with the error code if something doesn't work.


FULL LIST OF CONSOLE SCRIPT FLAGS
--------------------------------------------------------------------
  python auto_record_suno.py --help
  python auto_record_youtube.py --help

====================================================================


The rest of this file contains technical notes about the Windows
port: how the implementation differs from the Linux version
internally, what has and hasn't been verified, known limitations and
the history of fixes found during testing. None of this is required
reading for normal use of the program - it's aimed at people
maintaining or reviewing this Windows port.


## What changed technically (the Windows port)

The original version is built entirely on Linux-specific mechanisms
(PulseAudio for isolating a specific application's audio, `/proc` for
process lookup, x11grab/wf-recorder for screen capture). Windows has
neither, so:

- **Firefox audio isolation** is done through the native Windows API
  **WASAPI Process Loopback Capture** (introduced in Windows 10 2004+ /
  Windows 11) - the same mechanism used, for example, by OBS Studio's
  per-application audio capture. Implementation: `win_audio_loopback.py`.
- **Process lookup/termination** - through `win_process.py`
  (ctypes + `CreateToolhelp32Snapshot`, instead of `/proc`).
- **Firefox profile/binary lookup is simplified**: the program no
  longer requires the main Firefox to already be open and doesn't
  close it - the profile comes from
  `%APPDATA%\Mozilla\Firefox\profiles.ini`, the binary path from the
  registry (`App Paths`) / standard install folders / PATH. Firefox
  is always launched as a separate process with a profile copy.
- **Screen capture for video (YouTube)** - through `ffmpeg -f gdigrab`
  (`screen_capture.py`), instead of x11grab (X11) / wf-recorder
  (Wayland). Windows has no "display server" choice - gdigrab covers
  every case.
- **ffmpeg** is downloaded as a ready-made Windows build from gyan.dev
  when needed (instead of the static Linux build from
  johnvansickle.com - that site doesn't publish Windows builds).
- **Running without a console window, audio diagnostics, the
  "Fallback mode" checkbox** - see the relevant sections below; the
  Linux version either doesn't have these or doesn't need them for
  other reasons (it has a terminal, and PulseAudio-based isolation
  has no "fallback" mode - it either works or it doesn't).

## Verified on real Windows

Audio isolation (WASAPI Process Loopback), video capture (gdigrab), and the
Windows layer as a whole have been tried out on a real Windows machine - no
serious problems turned up. `win_audio_loopback.py`, `win_process.py` and
`screen_capture.py` were originally written from documented WinAPI/WASAPI/GDI
behavior (structures, GUIDs and signatures from public Microsoft documentation
and the official `ApplicationLoopback` C++ sample), and then verified in
practice.

If something does go wrong on your particular setup, please open an Issue in
the GitHub repository with the program's log (the "Check audio devices"
button / `--list-audio-devices` output is very helpful in a bug report) and
your Windows version. Also useful for diagnosis:
`python win_audio_loopback.py <firefox_PID> 5` - prints the capture-start
attempt log with HRESULT codes. As a fallback **without per-process
isolation** - the `--capture-mode device` CLI flag, or the "Fallback mode"
checkbox in the GUI.

## Known limitations

- Process Loopback Capture requires Windows 10 version 2004 (build
  19041) or newer, or Windows 11. On older systems, use
  `--capture-mode device`.
- `win_process.py::_wait_for_firefox_process` identifies "our"
  firefox.exe as a child process of the launched geckodriver - this
  is reliable under normal conditions, but could theoretically fail
  on non-standard Firefox builds that relaunch themselves through an
  intermediate launcher process.
- The ffmpeg auto-download points to a specific gyan.dev URL; if the
  build moves, update the constant in
  `auto_record_suno.py::_download_static_ffmpeg_windows` or use
  `--no-auto-ffmpeg` with your own `ffmpeg.exe` on PATH.
- Screen capture (gdigrab) records whatever is physically displayed
  on screen - if the player is small while the video is natively
  1080p/4K, the capture will still be player-sized (see the warning
  in the `auto_record_youtube.py` module docstring). The mouse cursor
  and windows on top of the player are captured too. DRM-protected
  video is recorded as black by gdigrab.

## History of fixes found while porting to Windows

Below is a timeline of issues found and fixed during development and testing
(useful if a similar error ever shows up in a log again):

1. **Crash on clicking "Stop" (exit code 3221225786 /
   `STATUS_CONTROL_C_EXIT`).** Fixed by switching to stopping the
   worker via `STOP` on stdin - see "Fixes: audio diagnostics and the
   second (console) window" below for the current stopping mechanism.
2. **`WinError 3` when caching the Firefox profile** (a path like
   `...\Firefox\1`). A `profiles.ini` parsing bug: the `Default`
   field is a PATH only in `[InstallXXXX]` sections; in `[ProfileN]`
   sections it's a `0`/`1` flag, and the path is in a separate `Path`
   field. Fixed.
3. **`'charmap' codec can't encode character...`** - the Windows
   console writes in the active code page by default, not UTF-8; a
   character outside it (e.g. in a track title) crashed `print()`
   PERMANENTLY at the same spot every time. `stdout`/`stderr` are now
   explicitly switched to UTF-8 with `errors="replace"`.
4. Also ported the **`atempo`** step (speed/pitch compensation for
   `--rate != 1.0`), which was missing in the Windows version, plus
   the diagnostic duration check on the recorded file - both existed
   in the original Linux version but were lost during the first port.

### Fixes: "recording doesn't happen" (log with 32 repeats of "internet is down")

1. **`[WinError -2147483634]` = `0x8000000E` (E_ILLEGAL_METHOD_CALL)
   when starting capture.** The `ActivateAudioInterfaceAsync`
   completion handler must be "agile" (answer
   `QueryInterface(IAgileObject)`); the `comtypes.COMObject`
   implementation didn't do that. `win_audio_loopback.py` was
   rewritten in pure ctypes (no `comtypes`): a custom COM object with
   IUnknown / IActivateAudioInterfaceCompletionHandler / IAgileObject,
   explicit vtable calls, and the HRESULT is returned as a number and
   turned into a `LoopbackCaptureError` naming the stage and code.
2. **A capture failure was reported as "internet is down".**
   `_looks_like_network_error` treated any `OSError` as a network
   error (and ctypes/WinAPI raise those too). Capture and WinAPI
   errors are no longer treated as network errors; neither is an
   `HTTPError` from geckodriver.
3. **An endless loop with no pauses.** After 3 consecutive failed
   capture start attempts, the program stops with a clear message
   (exit code 2) and a hint about `--capture-mode device`.
4. **Recording format matches Linux: WAV 96 kHz / 24-bit
   (pcm_s24le).** Capture runs at 96 kHz float32 (the Windows audio
   engine's native format, lossless), and ffmpeg writes WAV 96 kHz /
   24-bit. If the system doesn't accept 96 kHz, fallback formats are
   tried (48 kHz float32, 44.1 kHz/16-bit from Microsoft's sample) -
   the final file is still 96 kHz / 24-bit. FLAC again keeps the full
   96 kHz/24-bit (`-ar 96000`, same as Linux).
5. **Firefox's PID comes from `moz:processID`** (geckodriver's
   response), not "the first firefox.exe child process" - which could
   be a short-lived launcher.
6. **Silence during pauses.** WASAPI loopback doesn't send packets
   when there's no audio; idle periods are padded with zeros by wall
   clock (like a continuous parec stream).
7. **Diagnostics:** a warning is printed if only silence was
   recorded; the chosen format and the attempt log are printed to the
   log.
8. **Log lines arrived out of order** (stderr ahead of stdout): the
   PyInstaller build ignores `PYTHONUNBUFFERED`. `stdout` is now
   explicitly switched to line buffering.
9. Removed the `comtypes` dependency (and `--collect-all comtypes`
   from the build).

If capture doesn't start, run (on Windows, while a track is playing
in Firefox) `python win_audio_loopback.py <firefox_PID> 5` - it will
print the attempt log with HRESULT codes.

### Fixes: Firefox "abruptly closes", HTTP 500 / 404 errors

Symptoms from the logs: `Traceback ... HTTP Error 500: Internal
Server Error` right after startup, and/or an endless series of
`ERROR processing track: HTTP Error 404` after the browser closes.

1. **The program itself was closing Firefox on a page-load failure.**
   An `HTTP Error 500` on the first navigation to suno.com is actually
   geckodriver's "Reached error page: about:neterror…" response (no
   network / DNS / connection dropped). The actual reason is in the
   response BODY, but `urllib` only showed "Internal Server Error", so
   `navigate_with_retry` didn't recognize it as a network error,
   re-raised it, and `finally: session.quit()` closed Firefox. Now the
   response body is read (`WebDriverError`), the network error is
   recognized, the program waits for the internet and retries the
   navigation with an increasing delay - Firefox no longer closes.
2. **Automatic Firefox restart.** If the browser closed/crashed/lost
   its session (in the log this looks like `HTTP 500 … Failed to
   decode response from marionette`, then `404 invalid session id` on
   every command), the program no longer spins uselessly: it prints a
   report (Firefox's exit code, whether a crash-report file exists,
   the last lines of geckodriver's output), saves whatever had
   already been recorded of the current track, restarts Firefox and
   reopens the start page. You then need to press play again. The
   limit is 5 consecutive restarts without a single saved track
   (after that, exit code 3). The same logic is implemented for the
   YouTube worker.
3. **`is_pid_alive` lied.** While geckodriver still holds a handle on
   an exited firefox.exe, `OpenProcess` kept succeeding, so a closed
   Firefox was still considered "alive". The process exit code is now
   checked; `win_process.ProcessWatch` was added (a process handle
   plus Firefox's exit code, also guarding against killing an
   unrelated process if the PID gets reused).
4. **geckodriver's stderr wasn't being read**
   (`subprocess.PIPE` with nothing reading it): once the pipe buffer
   filled up, geckodriver and Firefox would block trying to write to
   the log. The output is now read on a separate thread (the last 200
   lines are kept for the report).
5. **Local requests to geckodriver weren't bypassing the system
   proxy** (VPN and proxy clients set one in Windows settings; urllib
   would pick it up even for 127.0.0.1).
6. **`wait_for_internet` is now time-bounded** (180 s): the check is
   done by the program itself (1.1.1.1 / 8.8.8.8 / suno.com) and can
   be wrong if Suno is only reachable through a VPN/proxy inside the
   browser - previously the program could hang waiting forever.
7. Audio capture failures after Firefox has closed no longer count
   toward "Windows is too old" (the 3-failed-attempts counter); simply
   waiting for play to be pressed (10 minutes) is no longer logged as
   an ERROR.
8. A **"Service"** block was added to the GUI under the header (Suno
   / YouTube - see below).

### Fixes: audio diagnostics and the second (console) window

**1. The "Check audio devices" button now actually works** (it used
to print a "not implemented" placeholder). Implementation:
`win_audio_devices.py`; the same is available from the CLI:
`python auto_record_suno.py --list-audio-devices` (and the same for
`auto_record_youtube.py`). The report includes:

- the Windows version and whether it's suitable for Process Loopback
  (needs build 19041+);
- OUTPUT devices (Core Audio, pure ctypes): name, type (speakers /
  headphones / HDMI…), state (active / disabled / not plugged in),
  the Windows mixer format, and a "DEFAULT" tag (console / multimedia
  / communications) - that's the device fallback mode listens to;
  INPUT devices are listed too;
- an ACTUAL test of starting capture using the same classes the
  recorder uses: process isolation (Process Loopback) and fallback
  mode - with the resulting format or an HRESULT error code;
- a summary with a recommendation (enable "Fallback mode", start the
  Windows Audio service, plug in a device, etc.).

The check runs on a background thread (the window doesn't freeze),
lines appear in the log as they become available, clicking again
while it's running is ignored, and it times out after 120 s.
Important: this checks that capture *initializes*; whether audio is
actually flowing is checked separately with:
`python win_audio_loopback.py <firefox_PID> 5`.

**2. The second, empty (console) window is gone.** Cause: the .exe
was built as a console application. There's no reliable way to hide
the console after the fact (PyInstaller's `--hide-console` doesn't
work on Windows 11 with Windows Terminal), so the build is now
**windowed** (`--noconsole` in `build-windows.sh`). Everything that
used to rely on the console has been reworked (`win_stdio.py`):

- **Stopping.** `CTRL_BREAK_EVENT` needs a shared console, and there
  isn't one anymore. The GUI now writes the string `STOP` to the
  worker's stdin (the `--control-stdin` flag); the worker raises a
  `KeyboardInterrupt` in response - the same `finally ->
  session.quit()` path, so Firefox closes normally. If the GUI
  crashes (the channel closes), the worker stops on its own too. If
  the worker hasn't responded within 20 s (180 s for YouTube - see
  below for why), the whole process tree is killed (`taskkill /T
  /F`), not just the onefile bootloader process.
- **Worker output.** In a windowed build, `sys.stdout/stderr/stdin`
  can be `None`. `ensure_std_streams()` picks up the real handles
  inherited from the GUI; if there are none, it writes a log to
  `%LOCALAPPDATA%\auto_record_suno\worker-stdio.log` (the GUI points
  you to that path if the log turns out empty).
- **Subprocesses without windows.** geckodriver, ffmpeg and taskkill
  are launched with `CREATE_NO_WINDOW` (otherwise each would pop up
  its own black window); their stdin defaults to DEVNULL, so ffmpeg
  doesn't "eat" the STOP command.
- Errors from UI event handlers and unhandled exceptions in the
  worker now show up in the log panel (with no console, nobody would
  see them otherwise).

When running from source (`python recorder_gui.py`), the terminal
console you launched it from is still there - that's your own
terminal window, not a second one from the program.

### Porting the YouTube (video) block from the Linux version

The YouTube block was ported "as is" (`auto_record_youtube.py`):
automatic video detection, maximizing the Firefox window and
attempting fullscreen, capturing the player area, waiting for the
`ended` event, recording without cutting out pauses, all 7 video
containers (mp4/mkv/webm/avi/mov/flv/3gp), "audio inside video" with
the same compatibility rules and fallbacks (webm→Opus, flv/3gp→AAC),
and audio-only recording when an audio container is selected. What
was replaced for the platform:

| Linux | Windows |
|---|---|
| audio: `parec` (PulseAudio) | WASAPI capture (`win_audio_loopback.py`) -> ffmpeg -> WAV 96kHz/24bit - same as Suno |
| video: `x11grab` / `wf-recorder` | `ffmpeg -f gdigrab` (`screen_capture.py`) |
| stopping video: SIGTERM | the `q` command on ffmpeg's stdin (otherwise the container would be left unfinished) |
| "Stop": SIGINT | the `STOP` string on the worker's stdin (`--control-stdin`) |
| picking an ffmpeg with libx264/libvpx | system -> cache -> gyan.dev build (`resolve_ffmpeg_bin_for_encoder`) |

Additionally (same as Windows-Suno): if Firefox closes or crashes, the
browser is restarted; on "Stop", the YouTube worker gets up to 180 s
(Suno gets 20 s) to encode and mux before the process is killed.

**DPI scaling.** The player area's coordinates are computed as
`mozInnerScreenX × devicePixelRatio` (physical pixels). The worker
calls `SetProcessDPIAware()`, and ffmpeg is launched with
`__COMPAT_LAYER=HIGHDPIAWARE` - otherwise, at 125-150% scaling,
gdigrab would capture a "virtualized" area instead. The area is also
clamped to the virtual screen's bounds.

Known quirks of screen capture: the mouse cursor ends up in frame (same as on
Linux); the AutoRecord window/other windows on top of the player get recorded
too; DRM-protected video is recorded as black by gdigrab. If the picture turns
out shifted or cropped at 125-150% display scaling or across multiple
monitors, please open an Issue with the "Screen capture area: ..." and "Video
capture started (gdigrab ...)" lines from the log, along with your screen
resolution and scaling.

## Where downloaded files are saved

- **ffmpeg and geckodriver** (things downloaded from the internet) go
  into an `AutoRecord-Codecs` folder RIGHT NEXT TO THE EXECUTABLE
  (portable mode). If that location isn't writable (e.g. the .exe
  sits in Program Files without admin rights) -
  `%LOCALAPPDATA%\auto_record_suno\codecs` is used as a fallback,
  with an explicit warning in the log.
- Downloads print the source (URL) and progress percentage (in 10%
  steps) - visible both in the console and in the GUI's log panel.
- **The Firefox profile copy and the firefox.exe path** (not
  downloaded - taken from this same computer) still live under
  `%LOCALAPPDATA%\auto_record_suno\`, as before.

## Building a distributable (portable .exe / .msi / Setup.exe)

`build-windows.sh` builds ready-made Windows packages **without a
real Windows machine** (through Wine + a portable Windows Python +
PyInstaller for the .exe itself, wixl for the .msi, makensis/NSIS for
Setup.exe - see the comments in the script itself for details and the
reasoning behind each step):

```bash
./build-windows.sh          # builds all three files into dist-windows/
./build-windows.sh --help   # list of flags (--portable-only, --no-msi, --no-exe-installer, etc.)
```

Output in `dist-windows/`:
- `AutoRecord-portable.exe` - a single file, installs nothing;
- `AutoRecord-Setup.msi` - a Windows Installer package (handy for
  silent/GPO deployment);
- `AutoRecord-Setup.exe` - a regular exe installer with the familiar
  wizard, registered in "Add or Remove Programs"/"Apps & features".

Requires Linux (Debian/Ubuntu, for auto-installing the wine/wixl/nsis
build dependencies).

The built .exe runs both the GUI (double-click) and both workers
internally (`AutoRecord.exe --run-worker ...` for Suno,
`AutoRecord.exe --run-worker-youtube ...` for YouTube) - `recorder_gui.py`
does this automatically; you never need to call these flags by hand.

## Files in this package

- `auto_record_suno.py` - Suno backend (CLI) and shared code (Firefox, ffmpeg, audio capture)
- `auto_record_youtube.py` - YouTube backend (CLI): audio and video
- `screen_capture.py` - screen capture for video (ffmpeg gdigrab, DPI, `q`-based stop)
- `recorder_gui.py` - graphical interface
- `win_audio_loopback.py` - pure-ctypes WASAPI audio capture (process isolation + fallback), self-test: `python win_audio_loopback.py <PID|device> [seconds]`
- `win_process.py` - Windows process handling
- `win_audio_devices.py` - audio device diagnostics ("Check audio devices" button, `--list-audio-devices`)
- `win_stdio.py` - running without a console window: std streams, windowless subprocesses, the STOP command over stdin
- `format_options.py`, `encode_helpers.py` - ffmpeg containers/encoding
- `i18n.py`, `gui_profiles.py` - GUI localization and settings profiles
- `requirements.txt` - no external dependencies (comment only)
- `build-windows.sh` - builds the portable .exe / .msi / Setup.exe without a Windows machine
