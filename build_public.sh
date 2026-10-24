#!/bin/bash
# 生成"公开版"站点到 dist/：不含技术指标分析（页面入口、tech_api、tech_config 都不带）
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist && mkdir dist
cp index.html dist/
for d in api valuation_api erp_api metals_api agri_api coal_api etf_api; do
  [ -d "$d" ] && cp -r "$d" dist/
done
# 删掉"技术指标分析"按钮
sed -i '/const tc=document.createElement/,/bar.appendChild(tc);/d' dist/index.html
if grep -q 'textContent="技术指标分析"' dist/index.html; then echo "错误：技术指标按钮没有删干净" >&2; exit 1; fi
if ls dist | grep -q '^tech'; then echo "错误：dist 里混进了 tech 目录" >&2; exit 1; fi
touch dist/.nojekyll
echo "公开版已生成：dist/（$(du -sh dist | cut -f1)）"
