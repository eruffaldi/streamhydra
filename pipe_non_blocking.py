# pipe_non_blocking.py (module)
"""
Example use:

    p = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            )

    pipe_non_blocking_set(p.stdout.fileno())

    try:
        data = os.read(p.stdout.fileno(), 1)
    except PortableBlockingIOError as ex:
        if not pipe_non_blocking_is_error_blocking(ex):
            raise ex
"""


__all__ = (
    "pipe_non_blocking_set",
    "pipe_non_blocking_is_error_blocking",
    "PortableBlockingIOError",
    )

import os


if os.name == "nt":
    def pipe_non_blocking_set(fd):
        # Constant could define globally but avoid polluting the name-space
        # thanks to: https://stackoverflow.com/questions/34504970
        import msvcrt

        from ctypes import windll, byref, wintypes, WinError, POINTER
        from ctypes.wintypes import HANDLE, DWORD, BOOL

        LPDWORD = POINTER(DWORD)

        PIPE_NOWAIT = wintypes.DWORD(0x00000001)

        def pipe_no_wait(pipefd):
            SetNamedPipeHandleState = windll.kernel32.SetNamedPipeHandleState
            SetNamedPipeHandleState.argtypes = [HANDLE, LPDWORD, LPDWORD, LPDWORD]
            SetNamedPipeHandleState.restype = BOOL

            h = msvcrt.get_osfhandle(pipefd)

            res = windll.kernel32.SetNamedPipeHandleState(h, byref(PIPE_NOWAIT), None, None)
            if res == 0:
                print(WinError())
                return False
            return True

        return pipe_no_wait(fd)

    def pipe_non_blocking_is_error_blocking(ex):
        if not isinstance(ex, PortableBlockingIOError):
            return False
        from ctypes import GetLastError
        ERROR_NO_DATA = 232

        return (GetLastError() == ERROR_NO_DATA)

    PortableBlockingIOError = OSError
else:
    def pipe_non_blocking_set(fd):
        import fcntl
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        return True

    def pipe_non_blocking_is_error_blocking(ex):
        if not isinstance(ex, PortableBlockingIOError):
            return False
        return True

    PortableBlockingIOError = Exception