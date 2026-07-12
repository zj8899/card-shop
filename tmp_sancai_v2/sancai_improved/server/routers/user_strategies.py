"""Strategy management endpoints — save, list, read source code."""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backtest.strategies.registry import list_all_strategies
from server.utils.response import ok as _ok

logger = logging.getLogger(__name__)
router = APIRouter()

USER_DIR = Path(__file__).parent.parent.parent / "backtest" / "strategies" / "user_generated"


class StrategySaveRequest(BaseModel):
    name: str = ""
    code: str = ""


def _ensure_user_dir():
    """Create user_generated directory and __init__.py if missing."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    init_file = USER_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text(
            '"""User-generated strategies auto-discovered by registry."""\n',
            encoding="utf-8",
        )


def _import_and_reload(name: str):
    """Re-import a user strategy module and refresh the registry."""
    try:
        import importlib
        from backtest.strategies.registry import _discover_user_strategies

        mod_name = f"backtest.strategies.user_generated.{name}"
        try:
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
        except ImportError:
            # First time import
            importlib.import_module(mod_name)

        # Re-scan the registry
        _discover_user_strategies()
        logger.info(f"Hot-reloaded user strategy: {name}")
    except Exception as e:
        logger.warning(f"Failed to reload strategy {name}: {e}")
        # Drop the (possibly broken) half-imported module from the cache so a
        # later retry re-executes it fresh instead of reusing a broken module.
        import sys
        sys.modules.pop(f"backtest.strategies.user_generated.{name}", None)
        # Don't raise — the strategy will load on next server restart


@router.post("/api/strategies/user")
async def save_strategy(req: StrategySaveRequest):
    """Save (create or overwrite) a user strategy after validation."""
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="策略名称不能为空")
    # Sanitize name: lowercase + replace hyphens/spaces with underscore
    req.name = req.name.strip().lower().replace("-", "_").replace(" ", "_")
    if not req.name:
        raise HTTPException(status_code=400, detail="策略名称无效")
    if not req.code or len(req.code.strip()) < 10:
        raise HTTPException(status_code=400, detail="策略代码至少需要10个字符")

    from server.routers.ai_chat import _validate_strategy_code

    # Validate syntax and safety
    validation = _validate_strategy_code(req.code, req.name)
    if not validation["valid"]:
        raise HTTPException(
            status_code=400,
            detail={"errors": validation["errors"], "warnings": validation.get("warnings", [])},
        )

    # Auto-fix: user_generated strategies need `from ..interface` not `from .interface`
    code = req.code.replace("from .interface import", "from ..interface import")
    code = code.replace("from .interface import", "from ..interface import")  # double ensure

    _ensure_user_dir()
    filepath = USER_DIR / f"{req.name}.py"
    if filepath.exists():
        logger.info(f"Overwriting existing strategy: {req.name}")

    filepath.write_text(code, encoding="utf-8")

    # Hot-reload into the registry
    _import_and_reload(req.name)

    return {
        "status": "saved",
        "name": req.name,
        "warnings": validation.get("warnings", []),
    }


def _resolve_user_strategy_file(name: str) -> Path | None:
    """Resolve a user strategy's real source file from its registry key.

    The registry key comes from the strategy class's `name` attribute (e.g.
    'user_one_yang_chuan'), which can differ from the on-disk filename (e.g.
    一阳穿.py, a Chinese name). Assuming key-minus-prefix == filename breaks for
    those, so resolve via the registered class's module __file__ instead.
    """
    import sys
    try:
        from backtest.strategies import registry
        key = name if name.startswith("user_") else f"user_{name}"
        cls = getattr(registry, "_registry", {}).get(key)
        if cls is None:
            return None
        mod = sys.modules.get(getattr(cls, "__module__", ""))
        fpath = getattr(mod, "__file__", None) if mod else None
        if fpath:
            p = Path(fpath).resolve()
            # Only accept files inside user_generated (path traversal guard)
            if str(p).startswith(str(USER_DIR.resolve())):
                return p
    except Exception:
        return None
    return None


@router.delete("/api/strategies/user/{name}")
async def delete_strategy(name: str):
    """Delete a user strategy (only user-created, not builtin)."""
    # ── Security: only allow deleting user strategies ──
    user_name = name.replace("user_", "", 1) if name.startswith("user_") else name

    # Validate name is safe (no path traversal)
    if ".." in user_name or "/" in user_name or "\\" in user_name:
        raise HTTPException(status_code=400, detail="Invalid strategy name")

    # Resolve real source file via registry (handles filename != registry key,
    # e.g. Chinese filenames). Fall back to the same-name file on disk.
    filepath = _resolve_user_strategy_file(name)
    if filepath is None:
        candidate = USER_DIR / f"{user_name}.py"
        if candidate.exists():
            filepath = candidate.resolve()

    if filepath is None or not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    # Verify it's actually in user_generated directory (path traversal guard)
    if not str(filepath.resolve()).startswith(str(USER_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Cannot delete strategies outside user_generated directory")

    stem = filepath.stem
    try:
        filepath.unlink()
        logger.info(f"Deleted user strategy: {stem} (key={name})")

        # Remove compiled cache files (keyed by the real file stem)
        for cache_dir in (USER_DIR / "__pycache__",):
            if cache_dir.exists():
                for cached in cache_dir.glob(f"{stem}.*"):
                    cached.unlink(missing_ok=True)

        # Remove from registry + refresh so the list updates immediately
        try:
            from backtest.strategies import registry
            if hasattr(registry, "_registry"):
                registry_key = name if name.startswith("user_") else f"user_{name}"
                registry._registry.pop(registry_key, None)
            # Re-scan so a stale in-memory module for this file is dropped
            if hasattr(registry, "_discover_user_strategies"):
                registry._discover_user_strategies()
        except Exception:
            pass

        return {"status": "deleted", "name": name}
    except Exception as e:
        logger.error(f"Failed to delete strategy {name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Unified strategy endpoints — used by ALL frontend modules
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/strategies/list")
async def list_all():
    """List ALL strategies (builtin + user) for dropdowns across all modules.

    Used by: backtest tab, K-line overlay, Didao scanner, AI assistant.
    """
    return _ok(list_all_strategies())


@router.get("/api/strategies/source/{name}")
async def get_source(name: str):
    """读取任意策略源码（内置+用户），用于策略管理页面的代码视窗."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid strategy name")
    import backtest.strategies as pkg
    pkg_dir = Path(pkg.__path__[0])

    # ── 1) User strategy file (resolve via registry, handles filename != key) ──
    resolved = _resolve_user_strategy_file(name)
    if resolved is None:
        user_name = name.replace("user_", "", 1) if name.startswith("user_") else name
        candidate = (USER_DIR / f"{user_name}.py").resolve()
        if str(candidate).startswith(str(USER_DIR.resolve())) and candidate.exists():
            resolved = candidate
    if resolved is not None and resolved.exists():
        return {
            "name": name, "code": resolved.read_text(encoding="utf-8"),
            "editable": True, "type": "user",
        }

    # ── 2) Builtin: map school ID to actual strategy file ──
    # Builtin strategy files in backtest/strategies/
    BUILTIN_MAP = {
        "simple": "simple_kdj",
        "simple_kdj": "simple_kdj",
        "strict": "strict_sancai",
        "strict_reverse": "strict_sancai",
        "strict_sancai": "strict_sancai",
        "schools": "school_ensemble",
        "school_ensemble": "school_ensemble",
        "single_school": "single_school",
        # For school IDs: source is in backtest/schools/<name>.py
        "chan_theory": "school_chan_theory",
        "ict": "school_ict",
        "price_action": "school_price_action",
        "wyckoff": "school_wyckoff",
        "morphology": "school_morphology",
        "gann": "school_gann",
        "wave_theory": "school_wave_theory",
        "dow_theory": "school_dow_theory",
    }

    mapped = BUILTIN_MAP.get(name, name)

    # Try as builtin strategies/ file
    for candidate in [mapped, name]:
        filepath = (pkg_dir / f"{candidate}.py").resolve()
        if str(filepath).startswith(str(pkg_dir.resolve())) and filepath.exists():
            return {
                "name": name, "code": filepath.read_text(encoding="utf-8"),
                "editable": False, "type": "builtin",
            }

    # ── 3) Try backtest/schools/ directory ──
    schools_dir = pkg_dir.parent / "schools"
    for candidate in [name, name.replace("school_", "")]:
        filepath = (schools_dir / f"{candidate}.py").resolve()
        if str(filepath).startswith(str(schools_dir.resolve())) and filepath.exists():
            return {
                "name": name, "code": filepath.read_text(encoding="utf-8"),
                "editable": False, "type": "builtin_school",
            }

    # ── 4) Backup files ──
    backup_dir = pkg_dir / "_backups"
    filepath = (backup_dir / f"{name}.py").resolve()
    if str(filepath).startswith(str(backup_dir.resolve())) and filepath.exists():
        return {
            "name": name, "code": filepath.read_text(encoding="utf-8"),
            "editable": False, "type": "backup",
        }

    raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
