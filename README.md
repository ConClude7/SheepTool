# SheepTool — 羊了个羊自动化助手

基于 Python 的「羊了个羊」游戏辅助工具，支持地图解析、自动求解与自动点击。

## 功能

- **地图抓取**：通过代理拦截 API 响应，自动获取关卡地图数据
- **地图解析**：将 API 响应 JSON 解析为结构化的牌局数据
- **自动求解**：多种求解算法（normal / random / level-top / level-bottom 等）
- **自动点击**：校准微信窗口后自动执行点击序列
- **校准工具**：交互式校准微信窗口中的牌局区域

## 环境要求

- macOS（依赖 macOS 窗口管理 API）
- Python 3.10+
- 微信桌面版

## 安装

```bash
# 克隆仓库
git clone <repo-url>
cd SheepTool

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

## 使用方法

### 1. 校准窗口

首次使用或微信窗口位置变化后，需要重新校准：

```bash
python main.py calibrate
```

校准完成后可以生成预览图检查准确性：

```bash
python main.py preview
```

### 2. 运行主流程

进入关卡后，从抓包工具或 Network 面板复制 API 响应 JSON，然后运行：

```bash
# 交互式粘贴 JSON
python main.py run

# 直接传入 JSON 字符串
python main.py run --json '{"data": ...}'

# 从文件读取
python main.py run --file response.json

# 指定关卡（默认第 2 关）
python main.py run --level 2

# 调整点击间隔（默认 0.4s）
python main.py run --delay 0.5

# 指定求解算法
python main.py run --algorithm random
```

### 运行期间快捷键

| 按键 | 功能 |
|------|------|
| `p`  | 暂停 / 继续 |
| `n`  | 下一步（单步模式）|
| `s`  | 结束运行 |

### 求解算法

| 算法 | 说明 |
|------|------|
| `normal` | 默认策略（推荐）|
| `random` | 随机多次尝试，取最优解 |
| `level-top` | 优先处理高层牌 |
| `level-bottom` | 优先处理低层牌 |
| `index-ascending` | 按索引升序 |
| `index-descending` | 按索引降序 |

## 配置

运行时配置保存在 `config.json`：

```json
{
  "click_delay": 0.4,
  "pause_after": 0,
  "algorithm": "normal",
  "solver": {
    "show_progress": true,
    "random_workers": 0,
    "solve_first": 0.8,
    "time_limit": -1
  }
}
```

## 项目结构

```
SheepTool/
├── main.py            # 主入口，命令行解析
├── calibrate.py       # 窗口校准
├── clicker.py         # 自动点击器
├── solver.py          # 求解器调度
├── intercept.py       # 网络拦截
├── proxy_manager.py   # 代理管理
├── map_fetcher.py     # 地图数据获取
├── map_parser.py      # 地图数据解析
├── macos_window.py    # macOS 窗口工具
├── config.json        # 运行配置
├── requirements.txt   # Python 依赖
├── tools/             # 求解器核心库
│   └── solver/
│       ├── business/  # 业务逻辑
│       ├── core/      # 核心数据结构
│       └── helper/    # 工具函数
└── data/              # 运行时数据（不纳入版本控制）
```

## 注意事项

- 本工具仅供学习与技术研究使用
- 使用前请确保微信窗口处于前台并已进入关卡界面
- 网络拦截功能需要在设备上安装并信任 mitmproxy 证书
