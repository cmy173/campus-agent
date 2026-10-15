"""Own process descendants without restricting files, networking, or code.

Our Node entrypoints wait for authenticated input before starting task children.
Attach the job before sending that input. On Windows, ordinary detached children
remain in the job; terminating only an ancestor PID is not sufficient.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from ctypes import wintypes as w


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('process_limit', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('times', ctypes.c_longlong * 4), ('faults', w.DWORD), ('total', w.DWORD),
                ('active', w.DWORD), ('terminated', w.DWORD)]


class ProcessOwner:
    def __init__(self):
        self.lock = threading.RLock()
        self.handle = None
        self.pid = None
        if os.name != 'nt':
            return
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        declarations = {
            'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in declarations.items():
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())

    def attach(self, proc: subprocess.Popen):
        self.pid = proc.pid
        if os.name != 'nt':
            return
        process = self.kernel.OpenProcess(0x0100 | 0x0001, False, proc.pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def active(self):
        with self.lock:
            if os.name != 'nt':
                if self.pid is None:
                    return 0
                try:
                    os.killpg(self.pid, 0)
                    return 1
                except ProcessLookupError:
                    return 0
            if not self.handle:
                return 0
            data = _Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(data), ctypes.sizeof(data), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return data.active

    def terminate(self, timeout=10):
        with self.lock:
            if os.name == 'nt':
                if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            elif self.pid is not None:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + timeout
            while self.active():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Owned processes have not stopped')
                time.sleep(0.02)

    def close(self):
        with self.lock:
            self.terminate()
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def spawn_owned(arguments, **options):
    owner = ProcessOwner()
    proc = None
    try:
        if os.name == 'nt':
            options.setdefault('creationflags', subprocess.CREATE_NO_WINDOW)
        else:
            options['start_new_session'] = True
        proc = subprocess.Popen(arguments, **options)
        owner.attach(proc)
        proc.campus_owner = owner
        def drain_on_exit():
            proc.wait()
            owner.terminate()
        threading.Thread(target=drain_on_exit, name='campus-owned-' + str(proc.pid), daemon=True).start()
        return proc
    except Exception:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        owner.close()
        raise
