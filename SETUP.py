import os
import platform
import shutil
import subprocess
import sys

DEPLOYMENT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(DEPLOYMENT_DIR, "app")

sys.path.insert(0, APP_DIR)
import audio_common as common

MIN_PYTHON = (3, 10)

REQUIRED_PACKAGES = ["mutagen", "librosa", "numpy", "scipy"]

# (script filename, launcher display name)
TOOLS = [
    ("clean_file_names.py", "___Step 1___ Clean File Names and Re-encode"),
    ("check_quality.py", "___Step 2___ Check Audio Quality"),
    ("filename_to_tag.py", "___Step 3___ Convert File Names to Tag"),
]


def ask_yes_no(question, default_yes=True):
    suffix = " [Y/n] " if default_yes else " [y/N] "
    answer = input(question + suffix).strip().lower()
    if answer == "":
        return default_yes
    return answer.startswith("y")


def check_python_version():
    print(f"Checking Python version... found {platform.python_version()}")
    if sys.version_info < MIN_PYTHON:
        needed = ".".join(str(part) for part in MIN_PYTHON)
        print(f"---- This needs Python {needed} or newer. ----")
        print("Please install a newer Python from https://www.python.org/downloads/")
        print("and run this setup again with it.")
        common.pause_exit(1)
    print("OK.\n")


def is_module_installed(module_name):
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def install_python_packages():
    missing = [pkg for pkg in REQUIRED_PACKAGES if not is_module_installed(pkg)]

    if not missing:
        print("All required Python packages are already installed.\n")
        return

    print("The following Python packages are needed but not installed:")
    for pkg in missing:
        print(" -", pkg)

    if not ask_yes_no("\nInstall them now with pip?"):
        print("Skipping. You can install them later with:")
        print(f"    {sys.executable} -m pip install {' '.join(missing)}\n")
        return

    print("\nInstalling, this can take a few minutes (librosa/numpy/scipy are large)...\n")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing], check=True)
        print("\nPackages installed successfully.\n")
    except subprocess.CalledProcessError:
        print("\n---- pip install failed, see the error above. ----")
        print("You can retry manually with:")
        print(f"    {sys.executable} -m pip install {' '.join(missing)}\n")


def try_install_ffmpeg_windows():
    if shutil.which("winget") is None:
        return False
    if not ask_yes_no("Install ffmpeg now with winget?"):
        return False
    try:
        subprocess.run(["winget", "install", "-e", "--id", "Gyan.FFmpeg"], check=True)
        return shutil.which("ffmpeg") is not None
    except subprocess.CalledProcessError:
        return False


def try_install_ffmpeg_macos():
    if shutil.which("brew") is None:
        return False
    if not ask_yes_no("Install ffmpeg now with Homebrew?"):
        return False
    try:
        subprocess.run(["brew", "install", "ffmpeg"], check=True)
        return shutil.which("ffmpeg") is not None
    except subprocess.CalledProcessError:
        return False


def try_install_ffmpeg_linux():
    package_managers = [
        ("apt", ["sudo", "apt", "update"], ["sudo", "apt", "install", "-y", "ffmpeg"]),
        ("dnf", None, ["sudo", "dnf", "install", "-y", "ffmpeg"]),
        ("pacman", None, ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"]),
    ]
    for manager, update_cmd, install_cmd in package_managers:
        if shutil.which(manager) is None:
            continue
        if not ask_yes_no(f"Install ffmpeg now with {manager} (needs sudo)?"):
            return False
        try:
            if update_cmd:
                subprocess.run(update_cmd, check=True)
            subprocess.run(install_cmd, check=True)
            return shutil.which("ffmpeg") is not None
        except subprocess.CalledProcessError:
            return False
    return False


def setup_ffmpeg():
    if common.check_ffmpeg_available("convert and analyze audio files"):
        print("ffmpeg is already installed.\n")
        return

    system = platform.system()
    installed = False
    if system == "Windows":
        installed = try_install_ffmpeg_windows()
    elif system == "Darwin":
        installed = try_install_ffmpeg_macos()
    elif system == "Linux":
        installed = try_install_ffmpeg_linux()

    if installed:
        print("ffmpeg installed successfully.\n")
    else:
        print("ffmpeg was not installed automatically. Please install it manually (see above) and re-run setup.\n")


def launcher_content(python_exe, script_path, system):
    if system == "Windows":
        return f'@echo off\r\n"{python_exe}" "{script_path}"\r\n'
    return f'#!/bin/bash\ncd "$(dirname "$0")"\n"{python_exe}" "{script_path}"\n'


def create_launcher(work_dir, automation_dir, script_name, display_name, system):
    script_path = os.path.join(automation_dir, script_name)
    python_exe = sys.executable

    if system == "Windows":
        launcher_path = os.path.join(work_dir, f"{display_name}.bat")
    elif system == "Darwin":
        launcher_path = os.path.join(work_dir, f"{display_name}.command")
    else:
        launcher_path = os.path.join(work_dir, f"{display_name}.sh")

    with open(launcher_path, "w", newline="") as f:
        f.write(launcher_content(python_exe, script_path, system))

    if system != "Windows":
        os.chmod(launcher_path, 0o755)

    return launcher_path


def create_launchers():
    work_dir = os.path.dirname(DEPLOYMENT_DIR)
    system = platform.system()

    print("Creating launchers in your work folder...")
    for script_name, display_name in TOOLS:
        launcher_path = create_launcher(work_dir, APP_DIR, script_name, display_name, system)
        print(" -", os.path.basename(launcher_path))
    print()


def main():
    print("==================== Riddim Automation Setup ====================\n")
    check_python_version()
    setup_ffmpeg()
    install_python_packages()
    create_launchers()
    print("==================== Setup complete! ====================")
    print("You can now double-click the launchers created in your work folder")
    print("to run each tool directly, any time.")
    common.pause_exit()


if __name__ == "__main__":
    common.run_main(main, ffmpeg_hint=True)