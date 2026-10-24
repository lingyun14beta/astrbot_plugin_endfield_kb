# 终末地知识库 · AstrBot 插件

本地 BM25 检索《明日方舟：终末地》的中文剧情、档案与整理资料。不依赖嵌入模型，不需要联网，索引与插件分离。

- 代码仓库：<https://github.com/lingyun14beta/astrbot_plugin_endfield_kb>
- 索引仓库：<https://github.com/lingyun14beta/astrbot-endfield-kb-index>

- 16,206 篇 / 340 万字，覆盖剧情原文、中枢档案、角色档案与语音、PRTS 整理资料
- 原文带说话人，整理资料的出处单独存为元数据
- 提供 LLM 工具、WebUI 控制台和 `zmd` 指令三种入口

## 特性

| 能力 | 说明 |
|---|---|
| 检索 | jieba 分词 + SQLite FTS5，BM25 排序；长原句、错字、OCR 噪声都能命中 |
| 检索单元 | 整篇文档（平均 200 余字，天然是一个场景），命中即可读全文 |
| 说话人 | 对话行入库时拼上 `speaker_raw`，旁白与黑屏文本没有说话人 |
| 出处 | `（dlg_e12m2_2）` 这类引用从正文剥离，按序存进 `refs`，详情面板可展开 |
| 索引管理 | 多版本共存、`current.json` 指针、完整性校验、原子替换 |
| 零外部依赖 | 不需要 embedding，不需要 API key |

## 安装

1. 把插件目录放到 `AstrBot/data/plugins/astrbot_plugin_endfield_kb/`
2. 安装依赖：`pip install jieba`（AstrBot 运行环境通常已自带）
3. 在 WebUI 插件页启用，或重启 AstrBot

## 准备索引

插件**不附带索引文件**。以下三种方式任选：

### A. 下载编译好的索引

从索引仓库的 Release 下载 `endfield_kb-latest.sqlite3.zip`：

```
https://github.com/lingyun14beta/astrbot-endfield-kb-index/releases/latest/download/endfield_kb-latest.sqlite3.zip
```

国内镜像：

```
https://www.modelscope.cn/api/v1/datasets/lingyun14/astrbot-endfield-kb-index/repo?Revision=master&FilePath=endfield_kb-latest.sqlite3.zip
```

解压后放进：

```
AstrBot/data/plugin_data/astrbot_plugin_endfield_kb/index/
```

### B. 手动导入

把 `.sqlite3` 或 `.zip` 直接丢进上面的目录，然后执行 `zmd 重载`。插件会自动解压、校验、切换。

### C. 本地构建

需要本机有 PRTS 语料发布包：

```bash
python build_index.py                    # 自动找最新语料，构建并安装
python build_index.py --corpus <目录>     # 指定语料目录
python build_index.py --out <文件>        # 只构建，用于打包发布
```

约 30–60 秒，产出 35 MB 左右的 SQLite 文件。

### 索引目录结构

```
data/plugin_data/astrbot_plugin_endfield_kb/index/
├─ current.json                          # 指针：当前文件与版本
├─ endfield_kb-endfield-1.6.3.sqlite3    # 当前索引
└─ endfield_kb-*.sqlite3.zip             # 待导入的压缩包（可选）
```

发现顺序：`current.json` → 目录内最新的库 → `.zip`/`.gz` 自动解压 → 旧路径迁移。

## 使用

### LLM 工具

`endfield_kb_search(query, top_k)`，由模型自行判断何时调用。返回带 `[原文]` / `[整理]` 标注的片段，整理类条目附「出处：…」。

### 指令

```
zmd 围陋村植禾      # 直接检索
zmd                # 索引状态
zmd 查 <关键词>    # 强制检索（查询词正好是保留词时）
zmd 重载           # 重新扫描本地索引
zmd 更新           # 手动更新说明
zmd 帮助
```

### WebUI 控制台

插件详情页打开「知识库控制台」：检索、查看完整正文、展开出处、切换暗色主题，竖屏自动重排。

控制台里还有两个按钮：

- **检查更新**：对比本地与远端版本；发现新版本时按钮变成「更新到 x.y.z」，再点一次即下载安装
- **导入索引**：选择本地 `.zip` / `.gz` / `.sqlite3`，上传后校验并立即生效

![控制台](docs/kb-console-desktop.png)

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `index_dir` | 空 | 索引目录；留空用 `data/plugin_data/astrbot_plugin_endfield_kb/index`，Docker 可指向挂载卷 |
| `manifest_urls` | GitHub + ModelScope | 更新清单地址，每行一个，按顺序尝试 |
| `index_urls` | GitHub + ModelScope | 清单不可用时直接下载的 zip 地址 |
| `enable_rewrite` | true | 长描述先用对话模型压成关键词；短关键词查询自动跳过 |
| `top_k` | 5 | 默认返回条数 |
| `max_chars` | 1800 | 单次返回正文上限 |
| `score_ratio` | 45 | BM25 相对阈值，越小越严格 |
| `rewrite_timeout` | 12 | 关键词改写超时（秒） |

## 后端接口

| 路由 | 方法 | 用途 |
|---|---|---|
| `/astrbot_plugin_endfield_kb/stats` | GET | 篇数、字数、体积、构建时间、语料版本、分类计数 |
| `/astrbot_plugin_endfield_kb/search` | GET | `q` 查询词、`k` 条数、`rewrite=1` 启用改写 |
| `/astrbot_plugin_endfield_kb/updates` | GET | 对比本地与远端版本 |
| `/astrbot_plugin_endfield_kb/update` | POST | 下载并安装最新索引 |
| `/astrbot_plugin_endfield_kb/upload` | POST | 接收手动上传的索引文件 |

## 设计取舍

- **整篇文档作为检索单元**：切块会打散场景，整篇更适合“命中即阅读”。
- **稀有词约束**：查询里 IDF 最高的一批词必须真的出现，否则“我/你/和”这类高频词会带进无关文档。
- **相对分数过滤**：强命中只回 1–2 条，弱命中才铺开候选。
- **原文优先**：整理资料标注 `[整理]`，只能当线索，引用以 `[原文]` 为准。
- **schema 版本**：索引写入 `schema_version`，插件不匹配时拒绝加载，避免半旧半新。

## 已知限制

- 只覆盖中文；多语言语料未接入
- 纯 BM25，完全口语化的复述会退化，长描述建议开启关键词改写
- 语料停在终末地 1.6.3，最新主线到《入丹青》
- 不含音频、美术、BGM；语料里只有台词的语音资源 id

## 许可

- **代码**：AGPL-3.0-or-later，见 [LICENSE](LICENSE)
- **索引文件**：不属于代码，不适用 AGPL；使用受数据仓库的 `LICENSE-INDEX.txt` 约束
- **游戏内容**：名称、文本及相关内容的权利归上海鹰角开拓者网络科技有限公司及相关权利人
- **整理内容**：权利归 PRTS.chat

数据来源：PRTS.chat 终末地资料集（release 1.6.3）
<https://modelscope.cn/datasets/HTiantian/prts-agent-corpus-endfield>

本项目为非官方社区项目，与游戏开发商、发行商无隶属或背书关系。
