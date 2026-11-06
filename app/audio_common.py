"""
Shared helpers for the numbered audio-processing scripts
(Clean File Names / Check Quality / Filename to Tag).
"""

import os
import shutil
import sys
import traceback


def script_folder(dunder_file):
    """Folder containing the calling script (pass it __file__)."""
    return os.path.dirname(os.path.abspath(dunder_file))


def work_folder(dunder_file):
    """User's work folder: two levels up from a script in app/ (app/ -> deployment folder -> work folder)."""
    return os.path.dirname(os.path.dirname(script_folder(dunder_file)))


def list_audio_files(folder_path, extensions):
    """Sorted top-level files in folder_path matching `extensions` (no recursion)."""
    return sorted(
        f for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f)) and f.lower().endswith(extensions)
    )


# Full path to qaac64.exe if it isn't on PATH (leave as-is when it is).
QAAC_EXE = "qaac64"


def find_qaac():
    """Path to qaac64 (QAAC_EXE as a file path, else looked up on PATH), or None."""
    if os.path.isfile(QAAC_EXE):
        return QAAC_EXE
    return shutil.which(QAAC_EXE) or shutil.which("qaac64") or shutil.which("qaac")


def pause_exit(code=None):
    """Standard 'press enter to close the console window' pause."""
    input("\n\n\nPress ENTER to exit")
    if code is not None:
        sys.exit(code)


def print_ffmpeg_install_help():
    print("To install ffmpeg and add it to PATH:")
    print("  Windows:")
    print("    1) winget install ffmpeg   (or download from https://www.gyan.dev/ffmpeg/builds/)")
    print("    2) If using the manual download, extract it and add the 'bin' folder")
    print("       (e.g. C:\\ffmpeg\\bin) to your PATH environment variable, then restart your terminal.")
    print("  macOS:")
    print("    brew install ffmpeg")
    print("  Linux (Debian/Ubuntu):")
    print("    sudo apt install ffmpeg")
    print("\nAfter installing, open a NEW terminal/command prompt and re-run this script.")


def check_ffmpeg_available(needed_for="convert files"):
    """True if ffmpeg is on PATH; otherwise prints install help and returns False."""
    if shutil.which("ffmpeg") is not None:
        return True
    print("---- ffmpeg was not found on your PATH ----")
    print(f"This is needed to {needed_for}.\n")
    print_ffmpeg_install_help()
    print()
    return False


def report_import_failure(pip_packages, needs_ffmpeg=False):
    """Call from a top-of-file `except` block: prints traceback + install instructions, then exits."""
    print(f"Imports failed:\n{traceback.format_exc()}")
    print(f"\nThis script needs: {pip_packages}")
    print(f"Install with:\n    pip install {pip_packages}")
    if needs_ffmpeg:
        print("\nThis script also needs ffmpeg available on your PATH.")
        if shutil.which("ffmpeg") is None:
            print("ffmpeg was NOT found on your PATH.")
            print_ffmpeg_install_help()
        else:
            print("(ffmpeg was found on PATH, so that's not the issue here.)")
    pause_exit(1)


def run_main(main_func, ffmpeg_hint=False):
    """Run main_func() under the standard top-level error handler (prints traceback, then pauses)."""
    try:
        main_func()
    except Exception:
        print("\n\n---- Something went wrong, here's the full error ----\n")
        print(traceback.format_exc())
        if ffmpeg_hint:
            print("If this mentions ffmpeg, make sure it's installed and on your PATH (see above),")
            print("or install it via 'winget install ffmpeg' on Windows, 'brew install ffmpeg' on macOS,")
            print("or 'sudo apt install ffmpeg' on Linux.")
        pause_exit()