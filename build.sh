#!/bin/bash
set -e

# 切換到腳本所在目錄
cd "$(dirname "$0")"

echo "🔨 正在清理舊的建置暫存檔..."
rm -rf build dist/*.spec

echo "📦 正在使用 PyInstaller 打包 AssetTrack..."
.venv/bin/pyinstaller --clean --onefile --name assettrack \
  --add-binary "assettrack/touchid_helper:assettrack" \
  entrypoint.py

echo "✅ 打包完成！執行檔位於：dist/assettrack"
