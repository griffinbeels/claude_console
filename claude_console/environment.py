"""The environment a freshly-launched process of yours gets, rebuilt from Windows.

A session opened by an app has to be indistinguishable from one you opened by
hand: cd to the project, open a terminal, run claude. It was not. An app that
spawns Claude sessions is very often started *from* a Claude session itself,
and Claude Code sets a batch of variables for the processes it spawns —
`NO_COLOR=1`, `GIT_EDITOR`, `GIT_TERMINAL_PROMPT=0`,
`CLAUDE_CODE_CHILD_SESSION`, and more. Popen inherits the spawning process's
environment, so every one of those was handed straight on: the new session
rendered monochrome, its git could not open an editor or ask for credentials,
and it kept no transcript.

Filtering the inherited environment cannot fix this, only chase it. The list
is upstream's and it grows. So don't filter — rebuild. `CreateEnvironmentBlock`
is what Windows itself uses to build the environment for a newly launched
process, straight from the machine and per-user registry, and it knows nothing
about whatever started this process. Reconstructing beats subtracting: a
variable added upstream tomorrow is absent by construction rather than by a
denylist nobody remembered to update.
"""

import ctypes
import os
from ctypes import wintypes

userenv = ctypes.WinDLL("userenv", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_QUERY = 0x0008

# Present in any real console, but left out of the block CreateEnvironmentBlock
# builds from a process token, so they are copied across explicitly. These say
# who you are; they carry nothing about the session that started the tracker.
IDENTITY_VARS = ("USERNAME", "USERDOMAIN")

# Without these the pseudo-handle comes back as a 32-bit int and the 64-bit
# callee reads garbage in the high half, failing with ERROR_INVALID_HANDLE.
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
userenv.CreateEnvironmentBlock.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]


def _parse_block(address: int) -> dict[str, str]:
    """Read Windows' NUL-separated, double-NUL-terminated environment block.

    Names keep the casing Windows stores them under (`Path`, `SystemRoot`)
    rather than the upper-cased form os.environ presents. Windows treats
    names case-insensitively, so this is only worth knowing when comparing
    the two — upper-case one side before looking anything up.
    """
    entries: dict[str, str] = {}
    while True:
        entry = ctypes.wstring_at(address)
        if not entry:
            return entries
        name, separator, value = entry.partition("=")
        # A leading '=' marks a per-drive current directory (`=C:=C:\repos`),
        # which is bookkeeping cmd.exe owns, not part of the environment.
        if name and separator:
            entries[name] = value
        # Advance by UTF-16 code units, not Python characters. Anything
        # outside the BMP is one character here and two units in the block, so
        # len() would leave the cursor short and every later entry misread.
        units = len(entry.encode("utf-16-le", "surrogatepass")) // 2
        address += (units + 1) * ctypes.sizeof(ctypes.c_wchar)


def login_environment() -> dict[str, str]:
    """Your environment as Windows would build it for a process launched now.

    Raises OSError if Windows will not produce one. That is deliberate: the
    caller surfaces it, rather than quietly falling back to the inherited
    environment and handing over a session that looks subtly wrong with
    nothing to say why.
    """
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    block = ctypes.c_void_p()
    try:
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            environment = _parse_block(block.value)
        finally:
            userenv.DestroyEnvironmentBlock(block)
    finally:
        kernel32.CloseHandle(token)

    present = {name.upper() for name in environment}
    for name in IDENTITY_VARS:
        if name not in present and name in os.environ:
            environment[name] = os.environ[name]
    return environment
