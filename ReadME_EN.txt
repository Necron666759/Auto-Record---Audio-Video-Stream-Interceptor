====================================================================
 Auto Record - Audio/Video Stream Interceptor
 User Guide (English)
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
both backends (Suno and YouTube) can also be run directly from a
terminal as plain Python scripts with their own flags (--help lists
all of them).

As of this version, the program can start the browser on its own
when you click "Start recording" - no need to have the main Firefox
already open every time. On the first successful run (while the
main Firefox was open at least once), it saves a dedicated
persistent copy of its profile and the firefox-bin path under
~/.cache/auto_record_suno/, and reuses that copy directly on every
following run. If the saved login session ever goes stale, the GUI
has a "Reset Firefox profile cache" button (or the
--reset-firefox-cache flag on the console scripts) - after that, the
next run will pick up a fresh profile from your open main Firefox
again.


PACKAGE CONTENTS
--------------------------------------------------------------------
  recorder_gui.py           - graphical interface (entry point)
  auto_record_suno.py       - Suno recording logic + all the shared
                               Firefox/audio handling code (also used
                               by the YouTube backend)
  auto_record_youtube.py    - YouTube recording logic (audio/video)
  screen_capture.py         - screen capture for video (X11/Wayland)
  format_options.py         - list of output formats/containers
  encode_helpers.py         - ffmpeg wrapper helpers
  gui_profiles.py           - saved GUI settings profiles
  i18n.py                   - interface localization (RU/EN)

  AppDir/, build-appimage.sh - AppImage build kit (see "AppImage"
                               section below)


REQUIREMENTS (what must be installed on the system)
--------------------------------------------------------------------
The program does NOT need anything installed via pip - only the
Python standard library. It does need the following system packages
and programs, though:

  1. Python 3.9+ with the tkinter module (for the GUI)
  2. ffmpeg (and ffprobe - usually shipped in the same package)
  3. PulseAudio utilities: pactl and parec
     (on PipeWire systems these are provided by the pipewire-pulse
     package - a drop-in compatible replacement, nothing extra to
     install if PipeWire is already set up as the PulseAudio
     replacement)
  4. The libpulse shared library (loaded directly via ctypes for
     more reliable audio device keep-alive monitoring)
  5. A real (NOT Snap, NOT Flatpak) Firefox build - the program
     needs an actual firefox-bin process, which it looks up via
     /proc. Snap/Flatpak Firefox builds run in a different namespace
     and/or binary layout, so the program may fail to find the
     right process. Firefox is NOT installed automatically by the
     package lists or by build-appimage.sh below - make sure you
     already have a suitable Firefox installation on your system.
  6. wf-recorder - ONLY if you plan to record YouTube VIDEO under a
     Wayland session (X11 doesn't need it: it uses plain
     ffmpeg -f x11grab). IMPORTANT: wf-recorder only works under
     wlroots-based compositors (Sway, Hyprland, etc). Video
     recording will NOT work under GNOME/KDE on Wayland - either
     record audio only, or switch to an X11/Xorg session at login.
  7. FUSE (libfuse2/fuse2) - required to run AppImages at all on
     some newer distributions (see below).

geckodriver does NOT need to be installed separately - on first run
the script downloads the right version from GitHub Releases into
~/.cache/auto_record_suno/geckodriver and reuses it afterwards (this
one-time download needs internet access).


PACKAGES BY DISTRIBUTION
--------------------------------------------------------------------

Debian / Ubuntu (apt):
  sudo apt update
  sudo apt install python3 python3-tk ffmpeg pulseaudio-utils \
      libpulse0 libfuse2 wf-recorder

  Notes:
   - On Ubuntu 24.04+ the package may be named libfuse2t64 instead
     of libfuse2 - apt will suggest the right name.
   - wf-recorder is only needed for Wayland video (see item 6 above).
   - Firefox itself is NOT installed by this list - see requirement
     5 above for what kind of Firefox build the program needs.

Fedora (dnf):
  sudo dnf install python3 python3-tkinter ffmpeg pulseaudio-utils \
      fuse fuse-libs wf-recorder

  Note: if the system ffmpeg was built without mp3/aac support
  (common without the RPM Fusion repo enabled), the program will try
  to download a static ffmpeg build with the needed codecs on its
  own - this one-time download needs internet access.

Arch Linux / Manjaro (pacman):
  sudo pacman -S python tk ffmpeg libpulse pulseaudio \
      fuse2 wf-recorder
  (if using PipeWire instead of PulseAudio:
   sudo pacman -S pipewire-pulse - provides pactl/parec)

openSUSE (zypper):
  sudo zypper install python3 python3-tk ffmpeg pulseaudio-utils \
      libpulse0 fuse wf-recorder


RUNNING WITHOUT APPIMAGE (directly)
--------------------------------------------------------------------
  python3 recorder_gui.py

All .py files must live in the same folder (they import each other
by relative path next to the script).


BUILDING AND RUNNING THE APPIMAGE
--------------------------------------------------------------------
The package includes a ready-made AppDir/ folder (already containing
all .py files, the AppRun launcher script, a .desktop file and an
icon) and a build-appimage.sh script that does two things in one
command:

  1) detects your distribution (Debian, Ubuntu, Fedora, RHEL/CentOS/
     Alma/Rocky, Arch/Manjaro, openSUSE - and their derivatives) and
     installs whatever is missing through apt/dnf/pacman/zypper
     (python3-tk, ffmpeg, pulseaudio-utils, libpulse, fuse, etc. -
     specific to your system; it does NOT install Firefox - see
     requirement 5 above);
  2) downloads appimagetool and builds the final
     Recorder-x86_64.AppImage file.

  chmod +x build-appimage.sh
  ./build-appimage.sh

Before installing anything, the script lists what's missing and asks
for confirmation (needs sudo if you're not root). Useful flags:

  ./build-appimage.sh --yes         skip the confirmation prompt,
                                     install packages and build right away
  ./build-appimage.sh --skip-deps   don't touch packages at all, just
                                     build the AppImage
  ./build-appimage.sh --deps-only   only check/install packages,
                                     don't build the AppImage
  ./build-appimage.sh --help        short flag reference

If the distribution can't be detected, the script skips the package
installation step and points you to installing them manually from
the list in this file (see the "PACKAGES BY DISTRIBUTION" section
above), then re-running with --skip-deps to just build the AppImage.

Both installing packages and building require internet access
(appimagetool is downloaded once and cached next to the script).

After building, simply run:
  ./Recorder-x86_64.AppImage

IMPORTANT about the AppImage: it does NOT bundle Python, ffmpeg,
Firefox, etc. inside itself - it uses whatever is already installed
on the system (see the sections above). This is a deliberate design
choice: the program talks directly to the system's PulseAudio/
PipeWire and to the system's Firefox via /proc, and neither of those
can be "packaged inside" an AppImage - they have to be part of the
host system. If some package is missing, AppRun will print a clear
message naming the package for your distribution instead of a
confusing Python traceback.

If running the AppImage fails with "cannot execute binary file" or
"permission denied" - make sure FUSE is installed (see the
requirements section above) and that the file is executable
(chmod +x Recorder-x86_64.AppImage).


FIREFOX PROFILE CACHE
--------------------------------------------------------------------
Location: ~/.cache/auto_record_suno/
  firefox_profile/            - persistent copy of the Firefox profile
  firefox_binary_path.txt     - remembered path to firefox-bin
  geckodriver/, ffmpeg/       - auto-downloaded helper tools

The very first recording run needs your main Firefox open - that's
where the program takes the profile and binary path from, once. Any
run after that no longer needs it.

If the saved Suno/YouTube login session goes stale (you get logged
out), click "Reset Firefox profile cache" in the GUI, log back in on
your main Firefox, and start a recording again - the cache will be
rebuilt from the fresh profile.


FULL LIST OF CONSOLE SCRIPT FLAGS
--------------------------------------------------------------------
  python3 auto_record_suno.py --help
  python3 auto_record_youtube.py --help
====================================================================
