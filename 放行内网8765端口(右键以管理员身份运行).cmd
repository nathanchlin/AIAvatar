@echo off
chcp 65001 >nul
echo 正在为「小汐 3D 实时语音助手」添加 Windows 防火墙入站放行规则 (TCP 8765 HTTP + TCP 8766 HTTPS)...
netsh advfirewall firewall delete rule name="Xiaoxi-Live-Avatar-8765" >nul 2>&1
netsh advfirewall firewall delete rule name="Xiaoxi-Live-Avatar-8766-HTTPS" >nul 2>&1
netsh advfirewall firewall add rule name="Xiaoxi-Live-Avatar-8765" dir=in action=allow protocol=TCP localport=8765 profile=any
netsh advfirewall firewall add rule name="Xiaoxi-Live-Avatar-8766-HTTPS" dir=in action=allow protocol=TCP localport=8766 profile=any
if %errorlevel%==0 (
    echo [成功] 已放行 TCP 8765 (HTTP) 与 TCP 8766 (HTTPS) 端口！
    echo        内网支持麦克风推荐地址：https://^<本机局域网IP^>:8766/
) else (
    echo [失败] 请右键本文件，选择「以管理员身份运行」！
)
pause
