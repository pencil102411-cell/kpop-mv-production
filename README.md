# K-pop / 偶像 MV 制作（Claude Code skill）

给 Claude Code 用的 MV 制作 skill：从策划、按拍分镜，到 AI 视频生成的跑法、挑片、按镜头号逐刀返修，再到歌词字幕、3D 重点字和成片交付。参考库来自 6 支女团 MV 的逐帧拆解，外加一支从策划做到成片的 AI MV 实战复盘（《Red Line》）。

## 包含什么

| 部分 | 内容 |
|---|---|
| `SKILL.md` | 流程 A 策划 / B 分镜 / C 动手做 / D 生成以后 / E 字幕，硬规则，常用词 |
| `references/` | 案例拆解、节奏 / 构图 / 运镜 / 转场 / 动效库、AI 生成逻辑（首帧图、提示词避坑、跑法选择）、看片与挑片、字幕与重点字、交付、实战复盘、术语表 |
| `templates/` | 策划案、单镜头 / 段落 / 整首分镜、挑片结果记录 |
| `rubrics/` | 方案评分、分镜检查清单 |
| `scripts/mvfx.py` | 剪辑效果命令行工具：闪切、AB 交替、频闪、白闪、甩镜、滚转、字卡、点名重影、按镜头表卡拍粗剪（带镜头号、缺素材占位卡）、节奏预览片、舞蹈参考片对齐歌速、截原曲片段 |

## 安装

把整个文件夹放到 `~/.claude/skills/kpop-mv-production/`（Windows 是 `C:\Users\你的用户名\.claude\skills\kpop-mv-production\`），不要只复制 `SKILL.md`。

`scripts/mvfx.py` 需要 Python 3.9+、`numpy`、`opencv-python`、`Pillow`，以及命令行能直接运行的 `ffmpeg`。用法见 `references/代码工具.md`。

## 没放进仓库的

`references/本机工具链.md`：记的是作者自己电脑和工作站的工具路径、地址和参数（视频生成工作站、画布、Blender、超分软件等），不上传。`SKILL.md` 在"要在本机真正跑东西"时会读它；你可以照自己的环境写一份放在同一位置，没有也不影响策划和分镜。
