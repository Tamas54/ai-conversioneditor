#!/usr/bin/env python3
"""Eldobható PostgreSQL-fürt a tesztekhez és a helyi futtatáshoz (Docker nélkül).

    with TempPostgres() as url: ...           # Pythonból
    python scripts/pgtemp.py                  # kiírja a DATABASE_URL-t, Ctrl+C leállítja

A szerver-binárisokat a /usr/lib/postgresql/*/bin-ben vagy a `pg_config --bindir` alatt keresi.
Rootként (felhős konténer) a fürt a `postgres` felhasználóként fut.
"""
from __future__ import annotations

import glob
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def _bindir() -> str:
    for cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(cand, "initdb")):
            return cand
    pg_config = shutil.which("pg_config")
    if pg_config:
        d = subprocess.check_output([pg_config, "--bindir"], text=True).strip()
        if os.path.exists(os.path.join(d, "initdb")):
            return d
    raise RuntimeError("PostgreSQL server binaries (initdb) not found; set TEST_DATABASE_URL instead")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempPostgres:
    def __init__(self, dbname: str = "aice") -> None:
        self.dbname = dbname
        self.bindir = _bindir()
        self.port = _free_port()
        self.dir = tempfile.mkdtemp(prefix="aice-pg-")
        self.as_root = os.geteuid() == 0
        self.user = "postgres" if self.as_root else pwd.getpwuid(os.geteuid()).pw_name

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = list(args)
        if self.as_root:
            cmd = ["runuser", "-u", "postgres", "--"] + cmd
        return subprocess.run(cmd, check=check, capture_output=True, text=True)

    def start(self) -> str:
        if self.as_root:
            shutil.chown(self.dir, "postgres", "postgres")
        data = os.path.join(self.dir, "data")
        self._run(f"{self.bindir}/initdb", "-D", data, "-A", "trust", "-U", self.user, "--no-sync", "-E", "UTF8")
        self._run(f"{self.bindir}/pg_ctl", "-D", data, "-w", "-l", os.path.join(self.dir, "log"), "-o",
                  f"-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1 -c fsync=off "
                  f"-c synchronous_commit=off -c full_page_writes=off", "start")
        for _ in range(50):
            if self._run(f"{self.bindir}/pg_isready", "-h", "127.0.0.1", "-p", str(self.port), check=False).returncode == 0:
                break
            time.sleep(0.1)
        self._run(f"{self.bindir}/createdb", "-h", "127.0.0.1", "-p", str(self.port), "-U", self.user, self.dbname)
        return self.url

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}@127.0.0.1:{self.port}/{self.dbname}"

    def stop(self) -> None:
        self._run(f"{self.bindir}/pg_ctl", "-D", os.path.join(self.dir, "data"), "-m", "immediate", "stop", check=False)
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


if __name__ == "__main__":
    pg = TempPostgres()
    print(pg.start(), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        pg.stop()
        sys.exit(0)
