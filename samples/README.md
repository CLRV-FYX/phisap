# 3.20.0 新官谱格式样例目录

本目录用于存放 Phigros **3.20.0** 新格式官谱的样例文件，供逆向新格式与回归验证使用。

**样例文件（`chart_at_*.json` / `rpe_chart_at_*.json`）属于游戏版权素材，已通过 `.gitignore` 排除在仓库之外**，仅保存在本地开发环境。如需补充新样例，直接覆盖同名文件即可：

| 文件 | 内容 |
| --- | --- |
| `chart_at_4159.json` | **纯官谱**：3.20.0 未经转换的原始官谱（formatVersion 3） |
| `rpe_chart_at_4159.json` | 同一张谱转换后的 RPE JSON（用于交叉验证） |
| `catalog_320.json` | （可选）3.20.0 的 `catalog.json` 原样内容 |
| `siblings_listing.txt` | （可选）同歌/同 bundle 旁边的文件清单 |
| `extra_notes.md` | （可选）其他文件的说明 |

验证方式：

```bash
python3 -m unittest tests.test_chart tests.test_catalog tests.test_rpe_import   # 解析与转换单元测试
python3 tools/verify_v3.py                # v3官谱 vs RPE转换谱交叉验证(默认读取上面两个样例)
python3 tools/verify_rpe_import.py        # RPE导入转换往返验证: RPE→v3 与官谱原谱逐点等价
```

## 已逆向出的 v3 格式要点（相对 v2）

1. `speedEvents` 移除 `floorPosition`：floor 从 0 按 `1.875*value/bpm` 逐段累积推导（同 v1）
2. 判定线移动事件的 y 分量改为标准屏幕坐标（0 在顶部），v2 为 0 在底部
3. 谱面文件名带 ` #<id>` 后缀（如 `Chart_AT #4159.json`），phisap 提取时自动规范化
4. 其余（音符类型编号 1tap/2drag/3hold/4flick、x=72px 单位、time=1/32 拍、旋转/透明度事件、每线 bpm）与 v2 一致
