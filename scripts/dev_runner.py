#!/usr/bin/env python3
"""Run CVTE-RAG-Plus application services locally with aggregated logs."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs" / "dev"
DOCKER_ENV = ROOT / "docker" / ".env"
BASE_COMPOSE = ROOT / "docker" / "docker-compose-base.yml"
ROOT_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
WORKER_PARSER_MODULES = (
    "audio.py",
    "book.py",
    "email.py",
    "laws.py",
    "manual.py",
    "naive.py",
    "one.py",
    "paper.py",
    "picture.py",
    "presentation.py",
    "qa.py",
    "resume.py",
    "table.py",
    "tag.py",
)
ANSI_ESCAPE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)
STOP_EVENT = threading.Event()
SIGINT_COUNT = 0


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def clean_line(line: str) -> str:
    return ANSI_ESCAPE.sub("", line.rstrip("\r\n"))


class LogSink:
    def __init__(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._all = self._open_log("all")
        self._service_logs: dict[str, object] = {}

    @staticmethod
    def _open_log(name: str):
        path = LOGS_DIR / f"{name}.log"
        return path.open("a", encoding="utf-8", buffering=1)

    def _get_service_log(self, service: str):
        writer = self._service_logs.get(service)
        if writer is None:
            writer = self._open_log(service)
            self._service_logs[service] = writer
        return writer

    def write(self, service: str, line: str) -> None:
        text = clean_line(line)
        if not text:
            return
        stamp = now_text()
        rendered = f"{stamp} [{service:<8}] {text}"
        with self._lock:
            print(rendered, flush=True)
            self._all.write(rendered + "\n")
            self._get_service_log(service).write(rendered + "\n")

    def banner(self, text: str) -> None:
        stamp = now_text()
        rendered = f"{stamp} [dev     ] {text}"
        with self._lock:
            print(rendered, flush=True)
            self._all.write(rendered + "\n")

    def close(self) -> None:
        with self._lock:
            for writer in self._service_logs.values():
                try:
                    writer.close()
                except Exception:
                    pass
            try:
                self._all.close()
            except Exception:
                pass


def read_base_python() -> Path:
    override = os.environ.get("CVTE_RAG_PLUS_BASE_PYTHON")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"CVTE_RAG_PLUS_BASE_PYTHON 不存在：{candidate}")

    pyvenv = ROOT / ".venv" / "pyvenv.cfg"
    if pyvenv.is_file():
        for raw_line in pyvenv.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw_line.lower().startswith("home") and "=" in raw_line:
                home = raw_line.split("=", 1)[1].strip().strip('"')
                candidate = Path(home) / "python.exe"
                if candidate.is_file():
                    return candidate

    fallback = ROOT / ".python-base" / "python.exe"
    if fallback.is_file():
        return fallback

    raise FileNotFoundError(
        "没有找到 uv 安装的 Python 3.10 解释器。"
        "请确认 .venv\\pyvenv.cfg 中的 home 路径仍然有效。"
    )


def prepend_env_path(existing: Optional[str], paths: Iterable[Path]) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    candidates = [str(path) for path in paths]
    if existing:
        candidates.extend(existing.split(os.pathsep))
    for candidate in candidates:
        value = candidate.strip()
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(value)
    return os.pathsep.join(ordered)


def common_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(name, None)
    cache_root = ROOT / ".cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
            "BROWSER": "none",
            "TIKTOKEN_CACHE_DIR": str(cache_root / "tiktoken"),
            "HF_HOME": str(cache_root / "huggingface"),
            "HF_HUB_CACHE": str(cache_root / "huggingface" / "hub"),
            "TORCH_HOME": str(cache_root / "torch"),
            "MODELSCOPE_CACHE": str(cache_root / "modelscope"),
            "NLTK_DATA": str(cache_root / "nltk"),
            "MPLCONFIGDIR": str(cache_root / "matplotlib"),
            "YOLO_CONFIG_DIR": str(cache_root / "ultralytics"),
            "XDG_CACHE_HOME": str(cache_root),
            "PIP_CACHE_DIR": str(cache_root / "pip"),
            "UV_CACHE_DIR": str(ROOT / ".uv-cache2"),
            "MINIO_HOST": "localhost",
            "MINIO_VISIT_HOST": "localhost",
            "MINIO_PUBLIC_ENDPOINT": "http://localhost:9000",
            "MINIO_CORS_ORIGINS": (
                "http://localhost:8000,http://localhost:3333,"
                "http://localhost,http://localhost:8888"
            ),
        }
    )
    return env


@dataclass
class Service:
    name: str
    cwd: Path
    command: list[str]
    env: dict[str, str]
    shell: bool = False
    process: Optional[subprocess.Popen] = None
    reader: Optional[threading.Thread] = None
    exit_reported: bool = False
    exit_code: Optional[int] = None


def python_service(
    name: str,
    cwd: Path,
    script: str,
    site_packages: Path,
    python_exe: Path,
    extra_args: Optional[list[str]] = None,
) -> Service:
    env = common_env()
    env["PYTHONPATH"] = prepend_env_path(
        os.environ.get("PYTHONPATH"), [site_packages, ROOT]
    )
    return Service(
        name=name,
        cwd=cwd,
        command=[str(python_exe), script, *(extra_args or [])],
        env=env,
    )


def node_service(
    name: str,
    cwd: Path,
    node_exe: Path,
    cli_script: Path,
    package_args: list[str],
    extra_env: Optional[dict[str, str]] = None,
) -> Service:
    env = common_env()
    if extra_env:
        env.update(extra_env)
    return Service(
        name=name,
        cwd=cwd,
        command=[str(node_exe), str(cli_script), *package_args],
        env=env,
    )


def resolve_node_cli(manager_path: Path, cli_relative_path: Path) -> Optional[Path]:
    candidate = manager_path.parent / cli_relative_path
    return candidate if candidate.is_file() else None


def start_service(service: Service, sink: LogSink) -> None:
    sink.banner(f"启动 {service.name}：{subprocess.list2cmdline(service.command)}")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    service.process = subprocess.Popen(
        service.command,
        cwd=str(service.cwd),
        env=service.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    sink.banner(f"{service.name} 已启动，PID={service.process.pid}")

    def pump_output() -> None:
        assert service.process is not None
        assert service.process.stdout is not None
        try:
            for line in service.process.stdout:
                sink.write(service.name, line)
        except Exception as exc:
            sink.write(service.name, f"读取日志失败：{exc}")

    service.reader = threading.Thread(
        target=pump_output,
        name=f"log-{service.name}",
        daemon=True,
    )
    service.reader.start()


def terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
    else:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=10)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def stop_services(services: list[Service], sink: LogSink) -> None:
    active = [svc for svc in services if svc.process and svc.process.poll() is None]
    if not active:
        return
    sink.banner("正在停止本地应用进程（Docker 基础设施保持运行）...")
    for service in reversed(active):
        assert service.process is not None
        terminate_tree(service.process)
    for service in services:
        if service.reader:
            service.reader.join(timeout=2)


def run_infra() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("未找到 docker，请先启动 Docker Desktop。")
    if not DOCKER_ENV.is_file():
        raise FileNotFoundError(f"缺少 Docker 配置文件：{DOCKER_ENV}")
    if not BASE_COMPOSE.is_file():
        raise FileNotFoundError(f"缺少 Docker Compose 文件：{BASE_COMPOSE}")

    prefix = [
        docker,
        "compose",
        "--env-file",
        str(DOCKER_ENV),
        "-f",
        str(BASE_COMPOSE),
    ]
    print(f"{now_text()} [docker  ] 启动 MySQL、Redis、MinIO、Elasticsearch...", flush=True)
    subprocess.run(
        [*prefix, "up", "-d", "--wait", "mysql", "redis", "minio", "es01"],
        cwd=str(ROOT),
        check=True,
    )
    print(f"{now_text()} [docker  ] 启动 Kibana...", flush=True)
    subprocess.run(
        [*prefix, "up", "-d", "kibana"],
        cwd=str(ROOT),
        check=True,
    )


def missing_worker_modules() -> list[Path]:
    app_dir = ROOT / "rag" / "app"
    return [app_dir / name for name in WORKER_PARSER_MODULES if not (app_dir / name).is_file()]


def stop_application_containers() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("未找到 docker，请先启动 Docker Desktop。")
    compose_file = ROOT / "docker" / "docker-compose.yml"
    if not compose_file.is_file():
        raise FileNotFoundError(f"缺少 Docker Compose 文件：{compose_file}")
    print(
        f"{now_text()} [docker  ] 停止 Docker 中的两个应用容器，释放本地端口...",
        flush=True,
    )
    subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(DOCKER_ENV),
            "-f",
            str(compose_file),
            "stop",
            "app",
            "management-frontend",
        ],
        cwd=str(ROOT),
        check=True,
    )


def build_services(
    python_exe: Path, include_backend: bool, include_worker: bool, include_frontend: bool
) -> list[Service]:
    if not python_exe.is_file():
        raise FileNotFoundError(f"Python 解释器不存在：{python_exe}")
    if not ROOT_SITE_PACKAGES.is_dir():
        raise FileNotFoundError(f"主服务依赖目录不存在：{ROOT_SITE_PACKAGES}")

    services: list[Service] = []
    if include_backend:
        services.extend(
            [
                python_service(
                    "api",
                    ROOT,
                    "api/server.py",
                    ROOT_SITE_PACKAGES,
                    python_exe,
                ),
                *(
                    [
                        python_service(
                            "worker",
                            ROOT,
                            "rag/svr/task_executor.py",
                            ROOT_SITE_PACKAGES,
                            python_exe,
                            ["0"],
                        )
                    ]
                    if include_worker
                    else []
                ),
            ]
        )

    if include_frontend:
        node = shutil.which("node.exe") or shutil.which("node")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        pnpm = shutil.which("pnpm.cmd") or shutil.which("pnpm")
        if not node:
            raise RuntimeError("未找到 node，请安装 Node.js。")
        if not npm:
            raise RuntimeError("未找到 npm，请安装 Node.js。")
        if not pnpm:
            raise RuntimeError("未找到 pnpm，请先安装 pnpm。")

        npm_cli = resolve_node_cli(
            Path(npm), Path("node_modules") / "npm" / "bin" / "npm-cli.js"
        )
        pnpm_cli = resolve_node_cli(
            Path(pnpm), Path("node_modules") / "pnpm" / "bin" / "pnpm.cjs"
        )
        if not npm_cli:
            raise RuntimeError(f"无法定位 npm-cli.js，npm 路径：{npm}")
        if not pnpm_cli:
            raise RuntimeError(f"无法定位 pnpm.cjs，pnpm 路径：{pnpm}")
        web_modules = ROOT / "web" / "node_modules"
        management_web_modules = ROOT / "management" / "web" / "node_modules"
        if not web_modules.is_dir():
            raise FileNotFoundError(
                f"前台依赖不存在：{web_modules}，请先在 web 目录运行 npm install。"
            )
        if not management_web_modules.is_dir():
            raise FileNotFoundError(
                "管理后台前端依赖不存在："
                f"{management_web_modules}，请先在 management\\web 运行 pnpm install。"
            )
        services.extend(
            [
                node_service(
                    "web",
                    ROOT / "web",
                    Path(node),
                    npm_cli,
                    ["run", "dev"],
                    {"PORT": "8000", "UMI_ENV": "development", "CHECK_TIMEOUT": "60"},
                ),
                node_service(
                    "mgmt-web",
                    ROOT / "management" / "web",
                    Path(node),
                    pnpm_cli,
                    ["dev"],
                    {"BROWSER": "none"},
                ),
            ]
        )
    return services


def install_signal_handlers() -> None:
    def handler(signum, _frame) -> None:
        global SIGINT_COUNT
        SIGINT_COUNT += 1
        if SIGINT_COUNT == 1:
            print(
                f"\n{now_text()} [dev     ] 收到停止信号，准备关闭应用进程...",
                flush=True,
            )
            STOP_EVENT.set()
        else:
            print(
                f"\n{now_text()} [dev     ] 再次收到停止信号，立即退出。",
                flush=True,
            )
            os._exit(130)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地运行 CVTE-RAG-Plus，Docker 只负责基础设施。"
    )
    parser.add_argument(
        "--skip-infra",
        action="store_true",
        help="不启动 Docker 基础设施，直接启动本地应用。",
    )
    parser.add_argument(
        "--infra-only",
        action="store_true",
        help="只启动 Docker 基础设施，不启动本地应用。",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="只启动统一 API 和 Worker，不启动两个前端。",
    )
    parser.add_argument(
        "--frontend-only",
        action="store_true",
        help="只启动前台 Web 和管理后台 Web。",
    )
    return parser.parse_args()


def main() -> int:
    configure_console()
    args = parse_args()
    install_signal_handlers()
    sink = LogSink()
    services: list[Service] = []
    exit_code = 0
    sink.banner("CVTE-RAG-Plus 本地开发启动器")
    sink.banner(f"日志目录：{LOGS_DIR}")

    try:
        python_exe = read_base_python()
        sink.banner(f"Python 解释器：{python_exe}")

        if not args.skip_infra:
            run_infra()
        else:
            sink.banner("已跳过 Docker 基础设施启动")

        if args.infra_only:
            sink.banner("基础设施已启动，按要求退出")
            return 0

        if not args.skip_infra:
            stop_application_containers()

        if args.no_frontend and args.frontend_only:
            raise ValueError("--no-frontend 和 --frontend-only 不能同时使用。")
        missing_modules = [] if args.frontend_only else missing_worker_modules()
        include_worker = not missing_modules
        if missing_modules:
            missing_text = ", ".join(
                str(path.relative_to(ROOT)) for path in missing_modules
            )
            sink.banner(
                "警告：当前代码缺少解析 Worker 所需模块，跳过 worker。"
                f"缺少：{missing_text}"
            )

        services = build_services(
            python_exe,
            include_backend=not args.frontend_only,
            include_worker=include_worker,
            include_frontend=not args.no_frontend,
        )
        if not services:
            raise ValueError("没有需要启动的服务。")
        for service in services:
            if STOP_EVENT.is_set():
                break
            start_service(service, sink)
            time.sleep(0.6)

        sink.banner("入口：前台 http://localhost:8000 | 管理后台 http://localhost:3333")
        sink.banner("入口：统一后端 http://localhost:9380")
        sink.banner("按 Ctrl+C 停止全部本地应用，Docker 基础设施会继续运行")

        while not STOP_EVENT.is_set():
            for service in services:
                if not service.process or service.exit_reported:
                    continue
                code = service.process.poll()
                if code is not None:
                    service.exit_code = code
                    service.exit_reported = True
                    sink.write(service.name, f"进程已退出，退出码：{code}")
                    if code != 0 and exit_code == 0:
                        exit_code = code
            if services and all(service.exit_reported for service in services):
                sink.banner("全部本地应用进程均已退出")
                break
            STOP_EVENT.wait(0.5)
        return 130 if STOP_EVENT.is_set() and SIGINT_COUNT else exit_code
    except KeyboardInterrupt:
        STOP_EVENT.set()
        return 130
    except subprocess.CalledProcessError as exc:
        sink.banner(f"Docker Compose 执行失败，退出码：{exc.returncode}")
        return exc.returncode or 1
    except Exception as exc:
        sink.banner(f"启动失败：{exc}")
        return 1
    finally:
        stop_services(services, sink)
        sink.banner("本地应用已停止")
        sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
