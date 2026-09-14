# Mac Storage Cleaner

扫描 Mac 磁盘空间，生成交互式 HTML 报告，查看目录占用、文件分类与大文件，并将选中的文件移入废纸篓。

使用 macOS 和 Python 3，依赖 Python 标准库。

```bash
python3 scan.py ~/Downloads
```

扫描完成后自动打开浏览器，服务地址为 `http://127.0.0.1:8432`。按 `Ctrl+C` 停止服务。省略路径时扫描当前用户主目录。

仅生成报告：

```bash
python3 scan.py ~/Downloads --no-serve
```

可用参数：

| 参数 | 作用 | 默认值 |
| --- | --- | --- |
| `path` | 扫描路径 | 当前用户主目录 |
| `--depth` | 最大扫描深度 | `6` |
| `--port` | 本地服务端口 | `8432` |
| `--no-serve` | 仅生成报告 | 关闭 |
| `--output` | 报告输出路径 | 脚本目录下的 `report.html` |

`report.html` 包含扫描所得的本机文件路径，已加入 Git 忽略规则。清理功能需要本地服务运行。
