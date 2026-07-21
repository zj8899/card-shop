"""
卡密商城 — 独立部署服务器
FastAPI + SQLite，VPS 一键启动
"""
import os
from pathlib import Path

# 加载 .env 文件（可选，不存在也不报错）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

from card_shop import router as shop_router

# ═══════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="卡密商城", version="1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# API 路由
app.include_router(shop_router, prefix="/api")

# ═══════════════════════════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return RedirectResponse(url="/shop")

@app.get("/shop")
async def shop_page():
    shop_path = static_dir / "shop.html"
    if shop_path.exists():
        return FileResponse(str(shop_path))
    return {"error": "shop.html not found"}

# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
