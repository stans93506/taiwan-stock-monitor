"""
本地執行：產生資料後自動開 HTTP server 並開啟瀏覽器
用法：python run_local.py
"""
import os, sys, subprocess, threading, time, webbrowser
import http.server, socketserver

PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_server():
    os.chdir(BASE_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *a: None  # 靜音 log
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    no_browser = "--no-browser" in sys.argv

    # 是否要先更新資料
    if "--update" in sys.argv or "-u" in sys.argv:
        print("正在執行 fetch_revenue.py 更新資料...")
        result = subprocess.run([sys.executable, "fetch_revenue.py"], cwd=BASE_DIR)
        if result.returncode != 0:
            print("fetch_revenue.py 執行失敗")
            sys.exit(1)

    # 背景啟動 HTTP server
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    print(f"HTTP server 啟動於 http://127.0.0.1:{PORT}/index.html")

    if no_browser:
        print("（--no-browser：跳過開啟瀏覽器，由 fetch_revenue.py 負責）")
    else:
        # 等一秒後開瀏覽器
        time.sleep(1)
        webbrowser.open(f"http://127.0.0.1:{PORT}/index.html")

    print("按 Ctrl+C 關閉 server")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Server 已關閉")
