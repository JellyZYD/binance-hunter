# 443 端口复用：网站 + Xray 翻墙共存

让同一台服务器、同一个 443 端口同时做两件事：

- 真实浏览器访问 `https://你的域名` → 看到 binance-hunter 网站
- 带正确 UUID 的 VLESS 客户端 → 走代理翻墙

做法是 **Xray 接管 443 并用你的证书终止 TLS**：翻墙流量被代理出去，非代理流量「回落」给本机 Nginx。对外只暴露成一个正常的 HTTPS 网站，伪装度最高。

```
翻墙客户端 ─VLESS+Vision─┐
                          ├─> Xray (443, 你的证书) ──┐
浏览器 https://域名 ──────┘                           ├─(翻墙)→ 直连外网
                                                      └─(回落)→ Nginx 127.0.0.1:8080 → Next 网站
```

> ⚠️ 安全提示：**UUID 相当于密码，不要提交到仓库**。下面用占位 `<YOUR-UUID>`，用 `xray uuid` 生成一个，服务端与客户端填同一个即可。

## 前置

- 已按 `README.md` 用自带证书完成整站 HTTPS 部署（证书在 `/etc/ssl/pixia/`，Nginx 当前持有 443）。
- 安全组已放行 80 / 443。

## 第 1 步：Nginx 从 443 挪到本机 8080（给 Xray 腾出 443）

```bash
cat <<'EOF' | sudo tee /etc/nginx/sites-available/binance-hunter > /dev/null
server {
    listen 80;
    server_name pixia.cc www.pixia.cc;
    return 301 https://$host$request_uri;
}

server {
    listen 127.0.0.1:8080;
    server_name pixia.cc www.pixia.cc;

    location /hunter-api/ {
        proxy_pass http://127.0.0.1:8787/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /health {
        proxy_pass http://127.0.0.1:8787/health;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
sudo nginx -t && sudo systemctl reload nginx
```

## 第 2 步：安装 Xray

```bash
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

## 第 3 步：让 Xray 以 root 运行（才能读证书私钥）

```bash
sudo mkdir -p /etc/systemd/system/xray.service.d
cat <<'EOF' | sudo tee /etc/systemd/system/xray.service.d/override.conf > /dev/null
[Service]
User=root
Group=root
EOF
```

## 第 4 步：写 Xray 配置（你的证书 + 回落到 8080）

先生成 UUID：`xray uuid`，把输出填到下面的 `<YOUR-UUID>`。

```bash
cat <<'EOF' | sudo tee /usr/local/etc/xray/config.json > /dev/null
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": {
        "clients": [
          { "id": "<YOUR-UUID>", "flow": "xtls-rprx-vision" }
        ],
        "decryption": "none",
        "fallbacks": [
          { "dest": "127.0.0.1:8080", "xver": 0 }
        ]
      },
      "streamSettings": {
        "network": "tcp",
        "security": "tls",
        "tlsSettings": {
          "minVersion": "1.2",
          "alpn": ["http/1.1"],
          "certificates": [
            {
              "certificateFile": "/etc/ssl/pixia/www.pixia.cc.pem",
              "keyFile": "/etc/ssl/pixia/www.pixia.cc.key"
            }
          ]
        }
      }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" }
  ]
}
EOF
```

记得把 `<YOUR-UUID>` 换成真实值。

## 第 5 步：启动并检查端口

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xray
sudo systemctl restart xray
sudo ss -ltnp | grep -E ':443|:8080|:80 '
```

期望：`*:443` → xray，`:80` 与 `127.0.0.1:8080` → nginx。

## 第 6 步：验证

```bash
curl https://pixia.cc/health        # 期望 {"ok": true}（经 Xray 回落到 nginx）
sudo systemctl status xray --no-pager | head -5
```

浏览器开 `https://pixia.cc` 网站正常；客户端选节点测延迟成功即翻墙就绪。

## 客户端（Clash Verge / Mihomo 内核）节点

```yaml
proxies:
  - name: "pixia-sg"
    type: vless
    server: pixia.cc
    port: 443
    uuid: <YOUR-UUID>          # 与服务端一致
    network: tcp
    tls: true
    udp: true
    servername: pixia.cc
    flow: xtls-rprx-vision
    client-fingerprint: chrome
```

## 维护

- **证书续期**：443 由 Xray 持有，换证书后执行 `sudo systemctl restart xray`（不是 reload nginx）。
- **不要重跑完整 `setup.sh`**：它会把 Nginx 重新生成回 `listen 443`，与 Xray 抢端口。日常更新用 `update.sh`（不动 Nginx）。万一跑了 setup.sh，重做第 1 步即可。
- **前端构建内存不足**（2 核 2G 偶发 `npm run build` 被 Killed）：加 2G swap 后重试。

  ```bash
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```

## 排查

- 节点延迟测试超时：服务端 `sudo journalctl -u xray -f`，客户端点一次延迟测试看有没有连接进来。
- 网站打不开但 `curl https://域名/health` 在服务器本机通：通常是回落或 8080 的 Nginx 配置问题，`sudo nginx -t` 检查。
