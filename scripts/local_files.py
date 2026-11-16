import ctypes as C
import os
import sys

from xhh_sdk.exceptions import XhhConfigError


def protect(path):
    if sys.platform != "win32":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    from ctypes import wintypes as W
    kernel = C.WinDLL("kernel32", use_last_error=True)
    security = C.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.LocalFree.argtypes = [C.c_void_p]
    security.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    security.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
    security.GetSecurityDescriptorDacl.argtypes = [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)]
    security.SetNamedSecurityInfoW.argtypes = [W.LPWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
    security.SetNamedSecurityInfoW.restype = W.DWORD
    token, sid_text, descriptor = W.HANDLE(), C.c_void_p(), C.c_void_p()
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)):
            raise OSError(C.get_last_error())
        size = W.DWORD()
        security.GetTokenInformation(token, 1, None, 0, C.byref(size))
        data = C.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, data, size, C.byref(size)):
            raise OSError(C.get_last_error())
        sid = C.cast(data, C.POINTER(C.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, C.byref(sid_text)):
            raise OSError(C.get_last_error())
        sddl = "D:P(A;OICI;FA;;;" + C.wstring_at(sid_text) + ")(A;OICI;FA;;;SY)"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None):
            raise OSError(C.get_last_error())
        present, defaulted, acl = W.BOOL(), W.BOOL(), C.c_void_p()
        if not security.GetSecurityDescriptorDacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)):
            raise OSError(C.get_last_error())
        result = security.SetNamedSecurityInfoW(str(path.resolve()), 1, 0x80000004, None, None, acl, None)
        if result:
            raise OSError(result)
    except OSError:
        raise XhhConfigError("private browsing storage permissions could not be established") from None
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(sid_text)
        if descriptor:
            kernel.LocalFree(descriptor)
