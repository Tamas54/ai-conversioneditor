"""Zárka: egy külső program (a videómotor) elzárva fut — csak a saját munkamappáját írhatja, a rendszerkönyvtárakat
olvashatja, hálózata nincs, új folyamatot nem indíthat, más folyamathoz nem nyúlhat, a környezeti változók (kulcsok)
nem jutnak át. Az ügynök saját EffectCraft-szkriptje (video, chart type "script") így futhat a szerveren.

Eszközök (rootjog és névterek nélkül — a Railway-konténerben az unshare tiltott, a Landlock és a seccomp nem):
  * Landlock (ABI ≥ 4): fájlrendszer-jogok útvonal szerint + TCP bind/connect tiltva; ABI ≥ 6: absztrakt
    unix-socket és jelküldés a zárkán belülre szűkítve. A ptrace a zárkán kívülre a Landlock miatt eleve tilos.
  * seccomp-BPF: socket(), fork/vfork, szálnak nem minősülő clone, ptrace, process_vm_*, io_uring, bpf, perf,
    kulcstartó, mount/unshare/setns… → EPERM (clone3 → ENOSYS, a glibc ilyenkor clone-ra vált).
  * rlimit: memória (címtér), fájlméret, nyitott fájlok, core dump nincs.
  * tiszta környezet: PATH, HOME/TMPDIR = munkamappa.

Használat: argv, env = command(exe, args, work=…); a hívó ezt indítja (cwd=work). Ha a kernel nem tud Landlockot,
available() hamis — megbízható munkára a hívó futtathat zárka nélkül, idegen szkriptet viszont NEM futtat.
A launcher maga ez a fájl: python -I -S jail.py <json> — csak a szabványos könyvtárat használja.
"""
from __future__ import annotations

import json
import os
import sys

# ---------- a hívó oldal ----------

_RO_DEFAULT = ("/usr", "/lib", "/lib64", "/lib32", "/etc/fonts", "/etc/ld.so.cache", "/etc/localtime",
               "/etc/ssl", "/dev/urandom", "/dev/random", "/sys/devices/system/cpu", "/proc/cpuinfo")
_ABI: list[int] = []


def abi() -> int:
    """A kernel Landlock-ABI-ja (0 = nincs)."""
    if not _ABI:
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype = ctypes.c_long
            v = libc.syscall(_NR_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))   # LANDLOCK_CREATE_RULESET_VERSION
            _ABI.append(max(0, int(v)))
        except Exception:
            _ABI.append(0)
    return _ABI[0]


def available() -> bool:
    return sys.platform.startswith("linux") and _machine() == "x86_64" and abi() >= 4


def _machine() -> str:
    return os.uname().machine


def command(exe: str, args: list[str], *, work: str, read: tuple[str, ...] = (), mem_gb: float = 6.0,
            env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """A zárkában futó parancs: [python, -I, -S, jail.py, <terv>] + a tiszta környezet."""
    exe = os.path.realpath(exe)
    linked = []                              # a munkamappába linkelt médiafájlok (stage): csak azok olvashatók
    for root, dirs, files in os.walk(work):
        for n in files + dirs:
            q = os.path.join(root, n)
            if os.path.islink(q):
                linked.append(os.path.realpath(q))
        if root != work:
            dirs[:] = []
    plan = {"exe": exe, "args": list(args), "work": os.path.realpath(work),
            "exec_dirs": [os.path.dirname(exe), os.path.dirname(os.path.dirname(exe))],
            "exec_files": sorted({os.path.realpath(p) for p in ("/lib64/ld-linux-x86-64.so.2",) if os.path.exists(p)}),
            "read": [p for p in (*_RO_DEFAULT, *read, *linked) if os.path.exists(p)], "mem": int(mem_gb * 2**30)}
    clean = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": plan["work"], "TMPDIR": plan["work"],
             "XDG_CACHE_HOME": os.path.join(plan["work"], ".cache"), "LANG": "C.UTF-8", **(env or {})}
    return [sys.executable, "-I", "-S", os.path.abspath(__file__), json.dumps(plan)], clean


# ---------- a zárka (launcher) ----------

_NR_CREATE, _NR_ADD, _NR_RESTRICT = 444, 445, 446
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REMOVE_DIR, FS_REMOVE_FILE, FS_MAKE_CHAR, FS_MAKE_DIR, FS_MAKE_REG = 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
FS_MAKE_SOCK, FS_MAKE_FIFO, FS_MAKE_BLOCK, FS_MAKE_SYM = 1 << 9, 1 << 10, 1 << 11, 1 << 12
FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1 << 0, 1 << 1


def _landlock(plan: dict) -> None:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    v = abi()
    if v < 4:
        raise SystemExit("jail: Landlock ABI >= 4 needed")
    fs_all = (1 << 13) - 1 | (FS_REFER if v >= 2 else 0) | (FS_TRUNCATE if v >= 3 else 0) | (FS_IOCTL_DEV if v >= 5 else 0)

    class Attr(ctypes.Structure):
        _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]

    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed", ctypes.c_uint64), ("fd", ctypes.c_int32)]

    attr = Attr(fs_all, NET_BIND_TCP | NET_CONNECT_TCP, (SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL) if v >= 6 else 0)
    size = ctypes.sizeof(Attr) if v >= 6 else 16
    rs = libc.syscall(_NR_CREATE, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if rs < 0:
        raise SystemExit(f"jail: landlock_create_ruleset errno {ctypes.get_errno()}")

    def allow(path: str, rights: int) -> None:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return
        try:
            if not os.path.isdir(path):      # fájlra csak fájl-jogok adhatók
                rights &= FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
            b = Beneath(rights & fs_all, fd)
            if libc.syscall(_NR_ADD, ctypes.c_int(rs), ctypes.c_int(1), ctypes.byref(b), ctypes.c_uint32(0)) < 0:
                raise SystemExit(f"jail: landlock_add_rule {path} errno {ctypes.get_errno()}")
        finally:
            os.close(fd)

    ro = FS_READ_FILE | FS_READ_DIR
    for p in plan["read"]:
        allow(p, ro)
    for p in plan["exec_dirs"]:
        allow(p, ro | FS_EXECUTE)
    for p in plan.get("exec_files", []):    # a dinamikus betöltő: az execve ezt is „futtatja”
        allow(p, FS_READ_FILE | FS_EXECUTE)
    allow("/dev/null", FS_READ_FILE | FS_WRITE_FILE)
    rw = fs_all & ~(FS_EXECUTE | FS_MAKE_CHAR | FS_MAKE_BLOCK | FS_IOCTL_DEV)
    allow(plan["work"], rw)
    if libc.prctl(38, 1, 0, 0, 0) != 0:                     # PR_SET_NO_NEW_PRIVS
        raise SystemExit("jail: no_new_privs failed")
    if libc.syscall(_NR_RESTRICT, ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
        raise SystemExit(f"jail: landlock_restrict_self errno {ctypes.get_errno()}")
    os.close(rs)


def _bpf() -> bytes:
    """seccomp-szűrő (x86_64): tiltólista → EPERM; clone csak szálnak; idegen ABI → a folyamat leáll."""
    import struct
    LD, JEQ, JGE, JSET, RET = 0x20, 0x15, 0x35, 0x45, 0x06
    ALLOW, KILL = 0x7FFF0000, 0x80000000
    ERR = lambda e: 0x00050000 | e
    deny = [41, 53, 57, 58, 101, 310, 311, 298, 321, 250, 248, 249, 425, 426, 427, 323, 165, 166, 272, 308, 155, 161,
            246, 320, 175, 313, 176, 169, 167, 168, 163, 312]
    # socket socketpair fork vfork ptrace process_vm_readv/writev perf_event_open bpf keyctl add_key request_key
    # io_uring_* userfaultfd mount umount2 unshare setns pivot_root chroot kexec_load kexec_file_load init_module
    # finit_module delete_module reboot swapon swapoff acct kcmp
    prog: list[tuple[int, int, int, int]] = [(LD, 0, 0, 4)]                       # arch
    prog.append((JEQ, 1, 0, 0xC000003E))                                          # AUDIT_ARCH_X86_64
    prog.append((RET, 0, 0, KILL))
    prog.append((LD, 0, 0, 0))                                                    # nr
    prog.append((JGE, 0, 1, 0x40000000))                                          # x32 ABI
    prog.append((RET, 0, 0, KILL))
    for nr in deny:
        prog.append((JEQ, 0, 1, nr))
        prog.append((RET, 0, 0, ERR(1)))
    prog.append((JEQ, 0, 1, 435))                                                 # clone3 → ENOSYS (glibc: clone)
    prog.append((RET, 0, 0, ERR(38)))
    prog.append((JEQ, 1, 0, 56))                                                  # clone?
    prog.append((RET, 0, 0, ALLOW))
    prog.append((LD, 0, 0, 16))                                                   # args[0] (flags, alsó 32 bit)
    prog.append((JSET, 1, 0, 0x00010000))                                         # CLONE_THREAD
    prog.append((RET, 0, 0, ERR(1)))
    prog.append((RET, 0, 0, ALLOW))
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog), len(prog)


def _seccomp() -> None:
    import ctypes
    raw, n = _bpf()
    buf = ctypes.create_string_buffer(raw, len(raw))

    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    libc = ctypes.CDLL(None, use_errno=True)
    p = Prog(n, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(22, 2, ctypes.byref(p), 0, 0) != 0:        # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
        raise SystemExit(f"jail: seccomp errno {ctypes.get_errno()}")


def _limits(plan: dict) -> None:
    import resource
    for res, val in ((resource.RLIMIT_AS, plan["mem"]), (resource.RLIMIT_FSIZE, 2 * 2**30),
                     (resource.RLIMIT_NOFILE, 512), (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):
            pass


def main() -> None:
    plan = json.loads(sys.argv[1])
    os.chdir(plan["work"])
    _limits(plan)
    _landlock(plan)
    _seccomp()
    os.execve(plan["exe"], [plan["exe"], *plan["args"]], dict(os.environ))


if __name__ == "__main__":
    main()
