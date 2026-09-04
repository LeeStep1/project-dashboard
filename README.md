# 李洋的项目看板（Project Dashboard）

面向项目经理（PM）的轻量级项目交付管控看板：以**售中交付时间线**为主视图，管理项目商务属性、评审/交底会议、后向供应商、待办进展与风险点；支持上传项目评审材料后由 AI 自动解析并回填项目信息。

- 后端：FastAPI + SQLite（零配置、自动建表迁移）
- 前端：单文件原生 HTML/CSS/JS（`static/index.html`），无需构建
- AI 能力：通过本机 [`arkcli`](https://www.volcengine.com/) 多模态理解解析评审材料（PDF / Word / 图片 / 扫描件）

## 功能特性

- **项目商务属性**：前向客户、项目简介、项目金额（元存储 / 万元展示）、利润率、运维承接组、是否提前实施、项目状态（实施中 / 已完成）、项目分类（平台类 / 硬件平台混合 / 硬件类）。
- **后向供应商**：供应商名称、采购方式（公开询比 / 直接采购 / 公开招标 / 原子能力下单）、采购内容，支持就地编辑。
- **会议记录**：技术评审会 / 项目评审会 / 交底会的日期登记与材料上传（每会议一个文件，重复上传自动替换）。
- **售中时间线**：按项目分类自动生成标准交付骨架（项目采购 → 后向合同签订 → 制定实施计划 → [硬件到货] → 项目实施 → 项目验收 → 项目交维），自动高亮当前步骤，逾期节点标红；会议节点并入同一时间线。
- **待办管理**：待办挂在具体交付步骤下，支持进度滑块、完成状态、截止日期（逾期提醒）、按时间线展示的进展记录（记录日期不填默认为当天）。
- **风险管控**：风险独立管理，支持等级（高/中/低）、解决状态与解决说明，未解决风险在卡片角标与顶部统计中预警。
- **AI 解析评审材料**：上传项目评审会材料后自动抽取项目属性、供应商及采购内容、会议日期、风险、待办，预览确认后才写入，可在预览中人工修改。
- **看板视图**：卡片摘要（客户 / 分类 / 金额 / 利润率 / 当前步骤 / 进度 / 未解决风险角标），实施中与已完成项目分组展示。

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 启动服务
uvicorn server:app --host 0.0.0.0 --port 8008
```

浏览器打开 <http://localhost:8008> 即可使用。数据库 `dashboard.db` 与上传目录 `static/uploads/` 在首次启动时自动创建。

### AI 解析功能（可选）

AI 解析依赖本机已登录的 `arkcli`（多模态理解）：

```bash
# 安装并登录 arkcli 后，服务端自动调用
arkcli auth login
```

未安装 / 未登录时，除 AI 解析外的所有功能均可正常使用。

## 部署为长期服务（macOS）

可使用 launchd 常驻：参考 `~/Library/LaunchAgents/` 下的 plist 配置，将工作目录指向本项目，执行：

```bash
launchctl load ~/Library/LaunchAgents/<your-plist>.plist
```

## 数据与隐私

- 所有数据保存在本地 SQLite 数据库（`dashboard.db`），上传文件保存在 `static/uploads/`，均不会被 git 跟踪。
- 项目评审材料仅在你主动点击「AI 解析」时发送给本机 `arkcli` 配置的模型服务。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | FastAPI、Uvicorn、Pydantic、SQLite3 |
| 前端 | 原生 HTML / CSS / JavaScript（单文件，无框架、无构建） |
| AI | arkcli 多模态理解（`+chat`） |

## 目录结构

```
project-dashboard-backend/
├── server.py              # FastAPI 后端：API、SQLite 自动迁移、AI 解析
├── requirements.txt       # Python 依赖
├── static/
│   ├── index.html         # 前端单页应用（看板 + 详情抽屉 + AI 预览弹窗）
│   └── uploads/           # 会议材料上传目录（运行时生成，不入库）
└── dashboard.db           # SQLite 数据库（运行时生成，不入库）
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 项目列表（含供应商、风险、待办、时间线） |
| POST / PATCH | `/api/projects`、`/api/projects/{id}` | 新建 / 更新项目商务属性 |
| POST / PATCH / DELETE | `/api/projects/{id}/suppliers`、`/api/suppliers/{id}` | 供应商增改删（含采购内容） |
| PUT / POST / DELETE | `/api/projects/{id}/meetings...` | 会议日期与材料文件 |
| POST / PUT / DELETE | `/api/projects/{id}/risks`、`/api/risks/{id}` | 风险增改删 |
| POST / PUT | `/api/projects/{id}/todos`、`/api/todos/{id}` | 待办（含里程碑归属、进度） |
| POST | `/api/todos/{id}/progress` | 待办进展记录 |
| POST / PUT / DELETE | `/api/timeline/...` | 时间线节点 |
| POST | `/api/projects/{id}/parse-review` | AI 解析评审材料 |

## 开源协议

[MIT License](LICENSE)
