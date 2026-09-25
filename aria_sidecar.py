#!/usr/bin/env python3
"""
aria_sidecar.py — Multi-instance FairPlay decrypt service manager.

Manages N sidecar instances for parallel ALAC decryption.
Each instance runs on its own port pair (decrypt + m3u8).

Architecture:
  1. One-time: sudo bind-mount /dev, /proc, /sys into rootfs
  2. Spawn N sidecar processes, each on ports base+i / base+10+i
  3. Per-instance health check + auto-restart with exponential backoff
  4. SIGUSR1 graceful restart of all instances
  5. SIGTERM/SIGINT clean shutdown + unmount

Usage:
    python3 aria_sidecar.py -F                       # 4 instances (default)
    python3 aria_sidecar.py -F --instances 2          # 2 instances
    python3 aria_sidecar.py -F --base-port 47010      # custom base port
    python3 aria_sidecar.py --login user:pass          # with Apple ID login
    python3 aria_sidecar.py --stub                     # dev/test stub mode

Port layout (--instances 4 --base-port 47010):
    inst-0: decrypt=47010  m3u8=47020
    inst-1: decrypt=47011  m3u8=47021
    inst-2: decrypt=47012  m3u8=47022
    inst-3: decrypt=47013  m3u8=47023
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# ─── Defaults ─────────────────────────────────────────────

DECRYPTOR_DIR    = Path("/mnt/data/codes/playground/am_alac_decryptor")
DEFAULT_WRAPPER  = str(DECRYPTOR_DIR / "sidecar")
DEFAULT_ROOTFS   = str(DECRYPTOR_DIR / "rootfs")
DEFAULT_BASE_PORT    = 47010
DEFAULT_M3U8_OFFSET  = 10      # m3u8 port = base + offset + instance
DEFAULT_INSTANCES    = 4
DEFAULT_GRACE_SECS   = 10
DEFAULT_WAIT_TIMEOUT = 90
MAX_RESTARTS         = 20
HEALTH_INTERVAL      = 30
SUDO_PASSWORD        = os.environ.get("SUDO_PASSWORD", "")

# ─── Logging ──────────────────────────────────────────────

class _Fmt(logging.Formatter):
    C = {
        "DEBUG": "\033[2m", "INFO": "\033[36m",
        "WARNING": "\033[33m", "ERROR": "\033[31m",
    }
    def format(self, r):
        c = self.C.get(r.levelname, "")
        ts = time.strftime("%H:%M:%S")
        return f"\033[2m{ts}\033[0m {c}\033[1m[{r.name}]\033[0m {r.getMessage()}"

_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_Fmt())

def _logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        lg.addHandler(_handler)
    return lg

log = _logger("sidecar")


# ─── Helpers ──────────────────────────────────────────────

def probe_port(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def sudo_run(cmd: list[str], check: bool = False, timeout: int = 10):
    return subprocess.run(
        ["sudo", "-S"] + cmd,
        input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=timeout,
    )


# ─── Mount Setup ─────────────────────────────────────────

def setup_rootfs(rootfs: str):
    r = Path(rootfs)
    for name in ["dev", "proc", "sys"]:
        mp = r / name
        mp.mkdir(exist_ok=True)
        chk = subprocess.run(["mountpoint", "-q", str(mp)], capture_output=True)
        if chk.returncode == 0:
            log.debug("%s already mounted", mp)
            continue
        res = sudo_run(["mount", "--bind", f"/{name}", str(mp)])
        if res.returncode == 0:
            log.info("mounted /%s → %s", name, mp)
        else:
            log.warning("mount /%s failed: %s", name, res.stderr.strip()[:80])

    urandom = r / "dev" / "urandom"
    if urandom.is_symlink():
        urandom.unlink()
        log.info("removed stale /dev/urandom symlink")

    etc = r / "etc"
    etc.mkdir(exist_ok=True)
    for name in ["resolv.conf", "hosts", "nsswitch.conf"]:
        src = Path(f"/etc/{name}")
        if src.exists():
            try:
                sudo_run(["cp", str(src), str(etc / name)])
            except Exception:
                pass

    log.info("rootfs ready")


def cleanup_rootfs(rootfs: str):
    for name in ["sys", "proc", "dev"]:
        sudo_run(["umount", "-l", str(Path(rootfs) / name)], check=False, timeout=5)


# ─── Single Instance ─────────────────────────────────────

class _Instance:
    """Manages one sidecar process."""

    def __init__(self, idx: int, *, wrapper: str, decrypt_port: int,
                 m3u8_port: int, login: str | None, code_from_file: bool,
                 stub_mode: bool, grace_secs: int, wait_timeout: int,
                 cwd: str):
        self.idx = idx
        self.tag = f"inst-{idx}"
        self.log = _logger(self.tag)
        self.wrapper = wrapper
        self.decrypt_port = decrypt_port
        self.m3u8_port = m3u8_port
        self.login = login
        self.code_from_file = code_from_file
        self.stub_mode = stub_mode
        self.grace_secs = grace_secs
        self.wait_timeout = wait_timeout
        self.cwd = cwd

        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._restart_flag = False
        self._restart_count = 0
        self._gen = 0
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _spawn(self) -> bool:
        self._gen += 1

        if self.stub_mode:
            return self._spawn_stub()

        cmd = [self.wrapper, "--userns",
               "--rootfs", str(DECRYPTOR_DIR / "rootfs"),
               "--bin", "/system/bin/main",
               "--wait-ports", f"{self.decrypt_port},{self.m3u8_port}",
               "--wait-timeout", "60",
               "--verbose",
               "--"]
        # child args (passed to /system/bin/main)
        cmd.extend(["-H", "127.0.0.1",
                    "-D", str(self.decrypt_port), "-M", str(self.m3u8_port)])
        if self.code_from_file:
            cmd.append("-F")
        if self.login:
            cmd.extend([f"--login={self.login}"])

        self.log.info("gen=%d spawn %d/%d", self._gen, self.decrypt_port, self.m3u8_port)

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=self.cwd, start_new_session=True,
            )
        except Exception as e:
            self.log.error("spawn failed: %s", e)
            return False

        threading.Thread(target=self._fwd, daemon=True, name=f"fwd-{self.idx}").start()
        self.log.info("pid=%d", self._proc.pid)
        return True

    def _spawn_stub(self):
        stub = None
        for c in [Path("/mnt/data/codes/aria/tools/stub_aria_daemon.py"),
                   DECRYPTOR_DIR / "tools" / "stub_aria_daemon.py"]:
            if c.exists():
                stub = str(c); break
        if not stub:
            self.log.error("stub not found"); return False
        self._proc = subprocess.Popen(
            [sys.executable, stub, "--host", "127.0.0.1",
             "--decrypt-port", str(self.decrypt_port),
             "--m3u8-port", str(self.m3u8_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self._fwd, daemon=True, name=f"fwd-{self.idx}").start()
        return True

    def _fwd(self):
        try:
            for line in iter(self._proc.stdout.readline, b""):
                msg = line.decode("utf-8", errors="replace").rstrip()
                if msg:
                    self.log.info("main: %s", msg)
        except Exception:
            pass

    def _wait_ready(self) -> bool:
        deadline = time.time() + self.wait_timeout
        d_up = m_up = False
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if self._proc and self._proc.poll() is not None:
                self.log.error("child exited during startup (code=%s)", self._proc.returncode)
                return False
            if not d_up and probe_port(self.decrypt_port):
                d_up = True
            if not m_up and probe_port(self.m3u8_port):
                m_up = True
            if d_up and m_up:
                self._restart_count = 0
                self._healthy = True
                self.log.info("ready — ports %d/%d", self.decrypt_port, self.m3u8_port)
                return True
            time.sleep(0.5)
        self.log.error("port timeout after %ds", self.wait_timeout)
        return False

    def is_healthy(self) -> bool:
        ok = probe_port(self.decrypt_port) and probe_port(self.m3u8_port)
        self._healthy = ok
        return ok

    def _kill(self):
        self._healthy = False
        if not self._proc or self._proc.poll() is not None:
            self._kill_orphans()
            return
        try:
            pgid = os.getpgid(self._proc.pid)
        except ProcessLookupError:
            pgid = self._proc.pid
        self.log.info("terminating pgid=%d", pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=self.grace_secs)
        except subprocess.TimeoutExpired:
            self.log.warning("grace expired → SIGKILL")
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
        self._kill_orphans()

    def _kill_orphans(self):
        for port in [self.decrypt_port, self.m3u8_port]:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"], text=True, timeout=3,
                ).strip()
                for pid_s in out.split("\n"):
                    pid = int(pid_s.strip())
                    if self._proc and pid == self._proc.pid:
                        continue
                    self.log.info("killing orphan pid=%d on :%d", pid, port)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except PermissionError:
                        sudo_run(["kill", "-9", str(pid)], check=False, timeout=3)
                    except ProcessLookupError:
                        pass
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                pass

    def request_restart(self):
        self._restart_flag = True

    def stop(self):
        self._stop.set()

    def run(self):
        """Main loop for this instance — runs in its own thread."""
        while not self._stop.is_set():
            if not self._spawn():
                self._restart_count += 1
                if self._restart_count >= MAX_RESTARTS:
                    self.log.error("max restarts reached"); return
                bk = min(60, 2 ** self._restart_count)
                self.log.info("retry %d/%d in %ds", self._restart_count, MAX_RESTARTS, bk)
                self._stop.wait(bk); continue

            if not self._wait_ready():
                self._kill()
                self._restart_count += 1
                if self._restart_count >= MAX_RESTARTS:
                    self.log.error("max restarts reached"); return
                bk = min(60, 2 ** self._restart_count)
                self.log.info("retry %d/%d in %ds", self._restart_count, MAX_RESTARTS, bk)
                self._stop.wait(bk); continue

            while not self._stop.is_set():
                self._stop.wait(HEALTH_INTERVAL)
                if self._stop.is_set(): break
                if self._restart_flag:
                    self._restart_flag = False
                    self.log.info("restarting (requested)")
                    self._kill(); break
                if self._proc.poll() is not None:
                    self.log.warning("child exited (code=%s)", self._proc.returncode)
                    break
                if not self.is_healthy():
                    self.log.warning("health check failed")
                    self._kill(); break

        self._kill()
        self.log.info("stopped")


# ─── Multi-Instance Manager ──────────────────────────────

class AriaSidecar:
    """Top-level manager: rootfs setup, N instances, signal handling."""

    def __init__(self, *, wrapper: str, rootfs: str,
                 base_port: int, m3u8_offset: int, num_instances: int,
                 login: str | None, code_from_file: bool,
                 stub_mode: bool, grace_secs: int, wait_timeout: int):
        self.wrapper = wrapper
        self.rootfs = rootfs
        self.num_instances = num_instances
        self.stub_mode = stub_mode
        self._stop = threading.Event()

        self.instances: list[_Instance] = []
        self.threads: list[threading.Thread] = []

        cwd = str(DECRYPTOR_DIR)
        for i in range(num_instances):
            dp = base_port + i
            mp = base_port + m3u8_offset + i
            inst = _Instance(
                i, wrapper=wrapper, decrypt_port=dp, m3u8_port=mp,
                login=login, code_from_file=code_from_file,
                stub_mode=stub_mode, grace_secs=grace_secs,
                wait_timeout=wait_timeout, cwd=cwd,
            )
            self.instances.append(inst)

    def _on_sig(self, signum, _):
        if signum == signal.SIGUSR1:
            log.info("SIGUSR1 → restart all instances")
            for inst in self.instances:
                inst.request_restart()
        else:
            log.info("signal %d → shutdown", signum)
            self._stop.set()
            for inst in self.instances:
                inst.stop()

    def run(self):
        signal.signal(signal.SIGTERM, self._on_sig)
        signal.signal(signal.SIGINT, self._on_sig)
        signal.signal(signal.SIGUSR1, self._on_sig)

        log.info("aria_sidecar starting — %d instances", self.num_instances)
        log.info("  wrapper: %s", self.wrapper)
        log.info("  rootfs:  %s", self.rootfs)
        for inst in self.instances:
            log.info("  %s: decrypt=%d m3u8=%d", inst.tag, inst.decrypt_port, inst.m3u8_port)

        if not self.stub_mode:
            if not Path(self.wrapper).is_file():
                log.error("wrapper not found: %s", self.wrapper)
                return
            setup_rootfs(self.rootfs)

        # Start each instance in its own thread
        for inst in self.instances:
            t = threading.Thread(target=inst.run, daemon=True, name=inst.tag)
            t.start()
            self.threads.append(t)
            time.sleep(1)  # stagger startups to avoid re-init conflicts

        # Wait for all to be ready (with timeout)
        deadline = time.time() + 120
        while time.time() < deadline and not self._stop.is_set():
            ready = sum(1 for inst in self.instances if inst.healthy)
            if ready == self.num_instances:
                log.info("all %d instances ready", self.num_instances)
                break
            time.sleep(1)
        else:
            ready = sum(1 for inst in self.instances if inst.healthy)
            if ready < self.num_instances:
                log.warning("only %d/%d instances ready", ready, self.num_instances)

        # Supervise — just wait for stop signal
        while not self._stop.is_set():
            self._stop.wait(60)
            if self._stop.is_set(): break
            alive = sum(1 for t in self.threads if t.is_alive())
            healthy = sum(1 for inst in self.instances if inst.healthy)
            log.info("status: %d/%d alive, %d/%d healthy",
                     alive, self.num_instances, healthy, self.num_instances)

        # Shutdown
        log.info("shutting down %d instances", self.num_instances)
        for inst in self.instances:
            inst.stop()
        for t in self.threads:
            t.join(timeout=15)

        if not self.stub_mode:
            cleanup_rootfs(self.rootfs)
        log.info("aria_sidecar stopped")


# ─── CLI ──────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="aria_sidecar — multi-instance FairPlay decrypt manager")
    p.add_argument("-n", "--instances", type=int, default=DEFAULT_INSTANCES,
                   help=f"Number of wrapper instances (default: {DEFAULT_INSTANCES})")
    p.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                   help=f"Base decrypt port (default: {DEFAULT_BASE_PORT})")
    p.add_argument("--m3u8-offset", type=int, default=DEFAULT_M3U8_OFFSET,
                   help=f"m3u8 port = base + offset + i (default: {DEFAULT_M3U8_OFFSET})")
    p.add_argument("-L", "--login", default=None, help="Apple ID login (user:pass)")
    p.add_argument("-F", "--code-from-file", action="store_true", default=True,
                   help="Read auth from files (default)")
    p.add_argument("--wrapper", default=DEFAULT_WRAPPER  = str(DECRYPTOR_DIR / "sidecar")
    p.add_argument("--rootfs", default=DEFAULT_ROOTFS, help="Path to rootfs")
    p.add_argument("--stub", action="store_true", help="Stub mode (no real decrypt)")
    p.add_argument("--grace-secs", type=int, default=DEFAULT_GRACE_SECS)
    p.add_argument("--wait-timeout", type=int, default=DEFAULT_WAIT_TIMEOUT)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    svc = AriaSidecar(
        wrapper=args.wrapper, rootfs=args.rootfs,
        base_port=args.base_port, m3u8_offset=args.m3u8_offset,
        num_instances=args.instances,
        login=args.login, code_from_file=args.code_from_file,
        stub_mode=args.stub, grace_secs=args.grace_secs,
        wait_timeout=args.wait_timeout,
    )
    svc.run()


if __name__ == "__main__":
    main()
