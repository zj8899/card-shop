# 卡密商城 — 独立部署版

> FastAPI + SQLite 自动售货系统：邮箱验证注册 + 充值卡密 + 自动提号

## 快速部署（VPS）

```bash
# 1. 克隆
git clone https://github.com/zj8899/card-shop.git
cd card-shop/deploy/card_shop

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置邮箱
cp .env.example .env
nano .env   # 填入 QQ邮箱 SMTP 信息

# 4. 初始化数据库
python init_db.py

# 5. 启动
python main.py
# → 访问 http://你的VPS的IP:8000/shop

# 管理员登录: admin / admin888
```

## 生产环境运行（推荐）

```bash
# 后台运行
nohup python main.py > app.log 2>&1 &

# 或用 systemd（自启动）
sudo nano /etc/systemd/system/card-shop.service
```

```ini
[Unit]
Description=Card Shop
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/card-shop/deploy/card_shop
EnvironmentFile=/root/card-shop/deploy/card_shop/.env
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable card-shop --now
```

## 反向代理（Nginx + 域名）

```nginx
server {
    listen 80;
    server_name 你的域名.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 文件结构

```
deploy/card_shop/
├── main.py            # FastAPI 入口
├── card_shop.py       # API 路由（24个接口）
├── init_db.py         # 数据库初始化
├── static/
│   └── shop.html      # 商城前端
├── data/
│   └── card_shop.db   # SQLite 数据库（自动创建）
├── requirements.txt   # Python 依赖
└── .env.example       # 环境变量模板
```
